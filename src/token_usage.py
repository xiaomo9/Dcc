"""本地上游 Token 用量；仅持久化模型标识和数字，不保存流量正文。"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import sqlite3
import time


_LOG_NAME = re.compile(r"^(\d{8})(?:\.(\d+))?\.jsonl$")
_PERIODS = {1, 7, 30, 90}


def _cutoff(days: int = 90) -> int:
    day = datetime.now().date() - timedelta(days=days - 1)
    return int(datetime.combine(day, datetime.min.time()).timestamp() * 1000)


def _usage(line: bytes | str, protocol: str) -> tuple[int | None, int | None] | None:
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    if not isinstance(line, str) or not line.startswith("data:") or "usage" not in line:
        return None
    try:
        event = json.loads(line[5:].strip())
    except ValueError:
        return None
    if not isinstance(event, dict):
        return None
    usage = event.get("usage")
    if not isinstance(usage, dict):
        for key in ("response", "message"):
            nested = event.get(key)
            if isinstance(nested, dict) and isinstance(nested.get("usage"), dict):
                usage = nested["usage"]
                break
    if not isinstance(usage, dict):
        return None

    def count(*names: str) -> int | None:
        for name in names:
            value = usage.get(name)
            if type(value) is int and value >= 0:
                return value
        return None

    input_tokens = count("input_tokens", "prompt_tokens")
    if protocol == "anthropic":
        cache_write = count("cache_creation_input_tokens")
        cache_read = count("cache_read_input_tokens")
        if any(value is not None for value in (input_tokens, cache_write, cache_read)):
            input_tokens = (input_tokens or 0) + (cache_write or 0) + (cache_read or 0)
    values = input_tokens, count("output_tokens", "completion_tokens")
    return values if any(value is not None for value in values) else None


class UsageStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS attempts ("
                       "id TEXT PRIMARY KEY, request_id TEXT NOT NULL, attempt_no INTEGER NOT NULL, "
                       "ts_ms INTEGER NOT NULL, model TEXT NOT NULL, slot TEXT NOT NULL, "
                       "protocol TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER, "
                       "source TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS attempts_ts ON attempts(ts_ms)")
            db.execute("CREATE TABLE IF NOT EXISTS import_cursors ("
                       "path TEXT PRIMARY KEY, inode INTEGER NOT NULL, offset INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS import_requests ("
                       "request_id TEXT PRIMARY KEY, attempt_no INTEGER NOT NULL, slot TEXT NOT NULL, "
                       "protocol TEXT NOT NULL, ts_ms INTEGER NOT NULL, actual INTEGER NOT NULL DEFAULT 0)")
            if "actual" not in {row[1] for row in db.execute("PRAGMA table_info(import_requests)")}:
                db.execute("ALTER TABLE import_requests ADD COLUMN actual INTEGER NOT NULL DEFAULT 0")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=1)
        os.chmod(self.path, 0o600)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _expire(db: sqlite3.Connection) -> None:
        cutoff = _cutoff()
        db.execute("DELETE FROM attempts WHERE ts_ms < ?", (cutoff,))
        db.execute("DELETE FROM import_requests WHERE ts_ms < ?", (cutoff,))

    def record(self, key: str, request_id: str, attempt_no: int, ts_ms: int,
               model: str, slot: str, protocol: str,
               input_tokens: int | None, output_tokens: int | None) -> None:
        with closing(self._connect()) as db, db:
            self._expire(db)
            db.execute("INSERT INTO attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'live') "
                       "ON CONFLICT(id) DO UPDATE SET "
                       "ts_ms=excluded.ts_ms, model=excluded.model, slot=excluded.slot, "
                       "protocol=excluded.protocol, "
                       "input_tokens=COALESCE(excluded.input_tokens, attempts.input_tokens), "
                       "output_tokens=COALESCE(excluded.output_tokens, attempts.output_tokens), "
                       "source='live'",
                       (key, request_id, attempt_no, ts_ms, model, slot, protocol,
                        input_tokens, output_tokens))

    def rows(self) -> list[sqlite3.Row]:
        with closing(self._connect()) as db, db:
            self._expire(db)
            return db.execute("SELECT * FROM attempts ORDER BY ts_ms, id").fetchall()

    def import_logs(self, log_dir: Path) -> None:
        if not log_dir.exists():
            return
        paths = sorted((p for p in log_dir.iterdir() if p.is_file() and _LOG_NAME.fullmatch(p.name)),
                       key=lambda p: (p.name[:8], int(_LOG_NAME.fullmatch(p.name).group(2) or 0)))
        with closing(self._connect()) as db, db:
            self._expire(db)
            for path in paths:
                stat = path.stat()
                cursor = db.execute("SELECT inode, offset FROM import_cursors WHERE path=?", (str(path),)).fetchone()
                offset = cursor["offset"] if cursor and cursor["inode"] == stat.st_ino and stat.st_size >= cursor["offset"] else 0
                if stat.st_size == offset:
                    continue
                pending = 0
                with path.open("rb") as stream:
                    stream.seek(offset)
                    while line := stream.readline():
                        if not line.endswith(b"\n"):
                            break
                        offset = stream.tell()
                        try:
                            event = json.loads(line)
                        except (UnicodeError, ValueError):
                            continue
                        if isinstance(event, dict):
                            self._import_event(db, event)
                        pending += 1
                        if pending >= 500:
                            db.execute("INSERT INTO import_cursors VALUES (?, ?, ?) "
                                       "ON CONFLICT(path) DO UPDATE SET inode=excluded.inode, offset=excluded.offset",
                                       (str(path), stat.st_ino, offset))
                            db.commit()
                            pending = 0
                db.execute("INSERT INTO import_cursors VALUES (?, ?, ?) "
                           "ON CONFLICT(path) DO UPDATE SET inode=excluded.inode, offset=excluded.offset",
                           (str(path), stat.st_ino, offset))

    @staticmethod
    def _import_event(db: sqlite3.Connection, event: dict) -> None:
        kind = event.get("kind")
        if kind not in ("cc_in", "gw_req", "gw_attempt", "gw_chunk"):
            return
        if kind == "gw_chunk":
            line = event.get("line")
            if not isinstance(line, (str, bytes)) or "usage" not in str(line):
                return
        req_id = event.get("req_id")
        if not isinstance(req_id, str) or not req_id:
            return
        try:
            ts_ms = int(datetime.fromisoformat(event["ts"]).timestamp() * 1000)
        except (KeyError, TypeError, ValueError):
            return
        if ts_ms < _cutoff():
            return
        state = db.execute("SELECT * FROM import_requests WHERE request_id=?", (req_id,)).fetchone()
        if kind == "cc_in":
            slot = event.get("slot") if isinstance(event.get("slot"), str) else ""
            protocol = event.get("protocol") if isinstance(event.get("protocol"), str) else ""
            db.execute("INSERT INTO import_requests VALUES (?, ?, ?, ?, ?, ?) "
                       "ON CONFLICT(request_id) DO UPDATE SET slot=excluded.slot, "
                       "protocol=excluded.protocol, ts_ms=excluded.ts_ms",
                       (req_id, state["attempt_no"] if state else 0, slot, protocol, ts_ms,
                        state["actual"] if state else 0))
        elif kind in ("gw_req", "gw_attempt"):
            if kind == "gw_req" and state and state["actual"]:
                return
            model = event.get("upstream_id")
            if not isinstance(model, str) or not model:
                return
            if state and kind == "gw_attempt" and not state["actual"] and state["attempt_no"]:
                attempt = state["attempt_no"]
            else:
                attempt = state["attempt_no"] + 1 if state else 1
            slot = state["slot"] if state else ""
            protocol = event.get("protocol") if isinstance(event.get("protocol"), str) else (state["protocol"] if state else "")
            db.execute("INSERT INTO import_requests VALUES (?, ?, ?, ?, ?, ?) "
                       "ON CONFLICT(request_id) DO UPDATE SET attempt_no=excluded.attempt_no, "
                       "slot=excluded.slot, protocol=excluded.protocol, ts_ms=excluded.ts_ms, actual=excluded.actual",
                       (req_id, attempt, slot, protocol, ts_ms, kind == "gw_attempt"))
            db.execute("INSERT OR IGNORE INTO attempts VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, 'log')",
                       (f"{req_id}:{attempt}", req_id, attempt, ts_ms, model, slot, protocol))
        elif kind == "gw_chunk" and state and state["attempt_no"]:
            values = _usage(event.get("line"), state["protocol"])
            if values is None:
                return
            db.execute("UPDATE attempts SET "
                       "input_tokens=CASE WHEN source='live' AND input_tokens IS NOT NULL "
                       "THEN input_tokens ELSE COALESCE(?, input_tokens) END, "
                       "output_tokens=CASE WHEN source='live' AND output_tokens IS NOT NULL "
                       "THEN output_tokens ELSE COALESCE(?, output_tokens) END "
                       "WHERE id=?", (*values, f"{req_id}:{state['attempt_no']}"))

    def summary(self, days: int) -> dict:
        if days not in _PERIODS:
            raise ValueError("统计时间范围无效")
        with closing(self._connect()) as db, db:
            self._expire(db)
            rows = db.execute("SELECT ts_ms, model, input_tokens, output_tokens "
                              "FROM attempts WHERE ts_ms >= ? ORDER BY ts_ms", (_cutoff(days),)).fetchall()
            first = db.execute("SELECT MIN(ts_ms) FROM attempts WHERE source='log'").fetchone()[0]

        def blank() -> dict:
            return {"input_tokens": 0, "output_tokens": 0, "attempts": 0,
                    "input_measured": 0, "output_measured": 0, "measured": 0, "unknown": 0}

        total = blank()
        models = defaultdict(blank)
        daily = {str(datetime.now().date() - timedelta(days=offset)): blank()
                 for offset in range(days - 1, -1, -1)}
        for row in rows:
            day = datetime.fromtimestamp(row["ts_ms"] / 1000).date().isoformat()
            for item in (total, models[row["model"]], daily[day]):
                item["attempts"] += 1
                item["input_measured"] += row["input_tokens"] is not None
                item["output_measured"] += row["output_tokens"] is not None
                item["measured"] += row["input_tokens"] is not None and row["output_tokens"] is not None
                item["unknown"] += row["input_tokens"] is None or row["output_tokens"] is None
                item["input_tokens"] += row["input_tokens"] or 0
                item["output_tokens"] += row["output_tokens"] or 0
        return {"days": days, "total": total,
                "models": [{"model": name, **values} for name, values in
                           sorted(models.items(), key=lambda pair: -(pair[1]["input_tokens"] + pair[1]["output_tokens"]))],
                "daily": [{"date": day, **daily[day]} for day in sorted(daily)],
                "imported_from": datetime.fromtimestamp(first / 1000).date().isoformat() if first else None}


class RequestUsage:
    def __init__(self, store: UsageStore):
        self.store = store
        self.request_id = ""
        self.slot = ""
        self.protocol = ""
        self.model = ""
        self.attempt = 0
        self.ts_ms = 0
        self.input_tokens = None
        self.output_tokens = None
        self.actual = False

    def observe(self, kind: str, fields: dict) -> None:
        if kind == "cc_in":
            self.request_id = fields.get("req_id") or ""
            self.slot = fields.get("slot") or ""
            self.protocol = fields.get("protocol") or ""
            self.attempt = 0
            self.actual = False
        elif kind in ("gw_req", "gw_attempt") and self.request_id:
            if kind == "gw_req" and self.actual:
                return
            if kind == "gw_attempt":
                if self.actual or self.attempt == 0:
                    self.attempt += 1
                self.actual = True
            else:
                self.attempt += 1
            self.model = fields.get("upstream_id") or ""
            if not self.model:
                return
            self.protocol = fields.get("protocol") or self.protocol
            self.ts_ms = int(time.time() * 1000)
            self.input_tokens = self.output_tokens = None
            self._save()
        elif kind == "gw_chunk" and self.attempt and self.model:
            values = _usage(fields.get("line"), self.protocol)
            if values is None:
                return
            self.input_tokens = values[0] if values[0] is not None else self.input_tokens
            self.output_tokens = values[1] if values[1] is not None else self.output_tokens
            self._save()

    def _save(self) -> None:
        self.store.record(f"{self.request_id}:{self.attempt}", self.request_id,
                          self.attempt, self.ts_ms, self.model, self.slot,
                          self.protocol, self.input_tokens, self.output_tokens)
