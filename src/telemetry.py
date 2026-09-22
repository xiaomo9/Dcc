"""精简使用事件；SQLite 跨进程队列，代理线程批量发送，不上传会话正文。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import requests

FIELDS = {
    'dcc_launch': {'slot', 'protocol', 'mode'},
    'dcc_proxy_start': {'result', 'durationMs'},
    'dcc_request_done': {'requestId', 'slot', 'protocol', 'model', 'stream', 'success',
                         'failReason', 'durationMs', 'firstEventMs', 'inputTokens',
                         'outputTokens', 'usageSource', 'candidateCount', 'switchCount'},
    'dcc_model_failover': {'requestId', 'protocol', 'fromModel', 'toModel', 'reason'},
}


class Telemetry:
    def __init__(self, directory: Path, settings: dict, version: str):
        self.path = directory / 'telemetry.sqlite3'
        self.settings = settings
        self.version = version
        self.enabled = settings.get('enabled', False) is True
        self.endpoint = settings.get('endpoint', '')
        self.lock = threading.Lock()
        self.wakeup = threading.Event()
        self.thread = None

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=0.1)
        try:
            conn.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)')
            conn.execute('CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, ts INTEGER, body TEXT)')
            conn.execute("INSERT OR IGNORE INTO meta VALUES ('clientId', ?)", (str(uuid.uuid4()),))
            conn.commit()
            return conn
        except Exception:
            conn.close()
            raise

    def track(self, name: str, **properties):
        if not self.enabled or name not in FIELDS:
            return
        conn = None
        try:
            conn = self._connect()
            now = int(time.time() * 1000)
            event = {
                'id': str(uuid.uuid4()), 'clientId': conn.execute(
                    "SELECT value FROM meta WHERE key='clientId'").fetchone()[0],
                'toolName': 'dcc', 'extVersion': self.version,
                'eventName': name, 'timestamp': now,
                'properties': {k: v for k, v in properties.items() if k in FIELDS[name]},
            }
            with conn:
                conn.execute('INSERT INTO events VALUES (?, ?, ?)',
                             (event['id'], now, json.dumps(event, ensure_ascii=False)))
                conn.execute('DELETE FROM events WHERE ts < ?', (now - 7 * 86400000,))
                conn.execute('DELETE FROM events WHERE id NOT IN '
                             '(SELECT id FROM events ORDER BY ts DESC, rowid DESC LIMIT 200)')
        except Exception:
            pass  # 埋点故障不得阻断业务，包括只读磁盘和并发锁冲突。
        finally:
            if conn is not None:
                conn.close()

    def flush(self):
        if not self.enabled or not self.endpoint or not self.lock.acquire(blocking=False):
            return False
        conn = None
        try:
            conn = self._connect()
            with conn:
                conn.execute('DELETE FROM events WHERE ts < ?',
                             (int(time.time() * 1000) - 7 * 86400000,))
            rows = conn.execute('SELECT id, body FROM events ORDER BY ts, rowid LIMIT 20').fetchall()
            if not rows:
                return True
            payload = {'batchId': str(uuid.uuid4()), 'sentAt': int(time.time() * 1000),
                       'events': [json.loads(row[1]) for row in rows]}
            # 不跟随重定向，防止将统计信息转发到意外地址。
            with requests.post(self.endpoint, json=payload, timeout=(2, 4),
                               allow_redirects=False) as response:
                if not 200 <= response.status_code < 300:
                    return False
                try:
                    result = response.json()
                except ValueError:
                    result = None
                if isinstance(result, dict) and result.get('code', 0) not in (0, '0'):
                    return False
            with conn:
                conn.executemany('DELETE FROM events WHERE id = ?', [(row[0],) for row in rows])
            return True
        except Exception:
            return False
        finally:
            if conn is not None:
                conn.close()
            self.lock.release()

    def start(self):
        if not self.enabled or not self.endpoint or self.thread is not None:
            return
        def worker():
            delay = 30
            while not self.wakeup.is_set():
                ok = self.flush()
                delay = 30 if ok else min(delay * 2, 300)
                self.wakeup.wait(delay)
        self.thread = threading.Thread(target=worker, name='dcc-telemetry', daemon=True)
        self.thread.start()

    def close(self):
        self.wakeup.set()


class RequestMetrics:
    """观察现有链路事件，正文仅在内存解析，只有白名单统计值进入队列。"""
    def __init__(self, telemetry: Telemetry):
        self.telemetry = telemetry
        self.started = time.monotonic()
        self.props = {}
        self.complete = False
        self.error = ''
        self.switch_reason = ''
        self.previous_model = ''
        self.first_event = None
        self.switches = 0

    def observe(self, kind, fields):
        if kind == 'cc_in':
            self.props.update(requestId=fields['req_id'], protocol=fields.get('protocol'),
                              slot=fields.get('slot'), model=fields.get('upstream_id'),
                              stream=fields.get('stream'), candidateCount=0)
        elif kind == 'gw_req':
            model = fields['upstream_id']
            if self.switch_reason:
                self.switches += 1
                self.telemetry.track('dcc_model_failover', requestId=self.props['requestId'],
                                     protocol=self.props['protocol'], fromModel=self.previous_model,
                                     toModel=model, reason=self.switch_reason)
                self.switch_reason = ''
            self.props['model'] = model
            self.props['candidateCount'] += 1
            self.complete, self.error = False, ''
            for key in ('inputTokens', 'outputTokens', 'usageSource'):
                self.props.pop(key, None)
        elif kind == 'gw_status':
            for key in ('inputTokens', 'outputTokens', 'usageSource'):
                self.props.pop(key, None)
            self.complete = False
            self.error = f"upstream_{fields['status']}" if fields['status'] >= 400 else ''
        elif kind == 'failover':
            self.switch_reason = fields['reason']
            self.previous_model = fields['from_id']
        elif kind == 'done' and fields.get('phase') == 'resolve':
            self.error = 'model_not_configured'
        elif kind == 'gw_chunk':
            self._chunk(fields['line'])

    def _chunk(self, line):
        if isinstance(line, bytes):
            line = line.decode('utf-8', errors='replace')
        if not line.startswith('data:'):
            return
        data = line[5:].strip()
        if data == '[DONE]':
            return  # 单独的 DONE 不能证明模型成功完成。
        try:
            event = json.loads(data)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        if self.first_event is None:
            self.first_event = round((time.monotonic() - self.started) * 1000)
        kind = event.get('type')
        if event.get('error') or kind in ('error', 'response.failed', 'response.incomplete'):
            self.error = 'upstream_event_error'
        protocol = self.props.get('protocol')
        if protocol == 'openai':
            self.complete |= any(c.get('finish_reason') is not None for c in event.get('choices', []))
        elif protocol == 'responses':
            self.complete |= kind == 'response.completed'
        elif protocol == 'anthropic':
            self.complete |= kind == 'message_stop'
        usage = event.get('usage') or event.get('response', {}).get('usage') or event.get('message', {}).get('usage')
        if isinstance(usage, dict):
            for key, sources in {'inputTokens': ('input_tokens', 'prompt_tokens'),
                                 'outputTokens': ('output_tokens', 'completion_tokens')}.items():
                for source in sources:
                    if isinstance(usage.get(source), int):
                        self.props[key] = usage[source]
                        self.props['usageSource'] = 'upstream'
                        break

    def finish(self):
        if not self.props:
            return
        reason = self.error or ('' if self.complete else 'incomplete_stream')
        self.telemetry.track('dcc_request_done', **self.props, success=not reason,
                             failReason=reason or None, firstEventMs=self.first_event,
                             durationMs=round((time.monotonic() - self.started) * 1000),
                             switchCount=self.switches)
