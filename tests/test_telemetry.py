import concurrent.futures
import http.server
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import proxy_server as proxy
from telemetry import Telemetry, RequestMetrics


class Receiver(http.server.BaseHTTPRequestHandler):
    batches = []
    status = 200
    code = 0
    protocol = 'openai'
    upstream_status = 200
    incomplete = False

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path == '/report':
            self.batches.append(body)
            self.send_response(self.status)
            self.end_headers()
            self.wfile.write(json.dumps({'code': self.code}).encode())
            return
        status = 429 if body.get("model") == "bad" else self.upstream_status
        self.send_response(status)
        self.end_headers()
        if status != 200:
            self.wfile.write(b'{"error":"private upstream error"}')
            return
        if self.protocol == 'openai':
            events = [{'choices': [{'delta': {'content': 'PRIVATE'}, 'finish_reason': None}]}]
            if not self.incomplete:
                events.append({'choices': [{'delta': {}, 'finish_reason': 'stop'}],
                               'usage': {'prompt_tokens': 10, 'completion_tokens': 2}})
        elif self.protocol == 'responses':
            events = [{'type': 'response.output_text.delta', 'delta': 'PRIVATE'}]
            if not self.incomplete:
                events.append({'type': 'response.completed', 'response': {
                    'usage': {'input_tokens': 10, 'output_tokens': 2}}})
        else:
            events = [{'type': 'message_start', 'message': {'id': 'm', 'role': 'assistant',
                       'model': 'mock', 'content': [], 'usage': {'input_tokens': 10}}},
                      {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'},
                       'usage': {'output_tokens': 2}}]
            if not self.incomplete:
                events.append({'type': 'message_stop'})
        for event in events:
            self.wfile.write(('event: ' + event.get('type', 'chunk') + '\ndata: ' +
                              json.dumps(event) + '\n\n').encode())


def serve(handler):
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.receiver = serve(Receiver)
        Receiver.batches = []
        Receiver.status, Receiver.code = 200, 0
        Receiver.upstream_status, Receiver.incomplete = 200, False
        self.base = f'http://127.0.0.1:{self.receiver.server_port}'
        self.usage = Telemetry(Path(self.tmp.name), {'enabled': True, 'endpoint': self.base + '/report'}, 'test')

    def tearDown(self):
        self.receiver.shutdown()
        self.receiver.server_close()
        self.tmp.cleanup()

    def events(self):
        with sqlite3.connect(self.usage.path) as db:
            return [json.loads(r[0]) for r in db.execute('SELECT body FROM events ORDER BY rowid')]

    def test_disabled_and_whitelist(self):
        off = Telemetry(Path(self.tmp.name) / 'off', {}, 'test')
        off.track('dcc_launch', slot='1')
        self.assertFalse(off.path.exists())
        self.usage.track('dcc_launch', slot='1', prompt='SECRET', api_key='SECRET')
        self.assertNotIn('SECRET', json.dumps(self.events()))

    def test_queue_retry_identity_and_batching(self):
        for _ in range(25):
            self.usage.track('dcc_launch', slot='1')
        before = self.events()
        Receiver.code = 1
        self.assertFalse(self.usage.flush())
        self.assertEqual(len(self.events()), 25)
        Receiver.code = 0
        self.assertTrue(self.usage.flush())
        self.assertEqual(len(self.events()), 5)
        self.assertEqual(Receiver.batches[0]['events'], Receiver.batches[1]['events'])
        self.assertEqual(len({e['clientId'] for e in before}), 1)
        self.assertEqual(len({e['id'] for e in before}), 25)
        self.assertTrue(self.usage.flush())
        self.assertEqual(self.events(), [])

    def test_concurrent_writers_and_cap(self):
        def track(_):
            Telemetry(Path(self.tmp.name), {'enabled': True}, 'test').track('dcc_launch', slot='1')
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(track, range(40)))
        self.assertEqual(len(self.events()), 40)
        self.assertEqual(len({e['clientId'] for e in self.events()}), 1)
        for _ in range(180):
            track(None)
        self.assertEqual(len(self.events()), 200)

    def test_failover_and_terminal_error(self):
        metric = RequestMetrics(self.usage)
        metric.observe('cc_in', {'req_id': 'id', 'protocol': 'openai', 'slot': '1'})
        metric.observe('gw_req', {'upstream_id': 'a'})
        metric.observe('failover', {'from_id': 'a', 'reason': 'soft_timeout'})
        metric.observe('gw_req', {'upstream_id': 'b'})
        metric.observe('gw_chunk', {'line': 'data: {"choices":[{"finish_reason":"stop"}]}'})
        metric.error = 'client_disconnected'
        metric.finish()
        events = self.events()
        self.assertEqual(events[0]['properties']['toModel'], 'b')
        self.assertFalse(events[1]['properties']['success'])
        self.assertEqual(events[1]['properties']['candidateCount'], 2)

    def test_send_failure_disk_failure_and_new_event_during_flush(self):
        self.usage.track('dcc_launch', slot='1')
        with patch('telemetry.requests.post', side_effect=requests.ConnectionError):
            self.assertFalse(self.usage.flush())
        self.assertEqual(len(self.events()), 1)
        with patch.object(self.usage, '_connect', side_effect=OSError):
            self.usage.track('dcc_launch', slot='2')
            self.assertFalse(self.usage.flush())
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = 200
        response.json.return_value = {'code': 0}
        def post(*args, **kwargs):
            self.usage.track('dcc_launch', slot='3')
            return response
        with patch('telemetry.requests.post', side_effect=post):
            self.assertTrue(self.usage.flush())
        self.assertEqual([e['properties']['slot'] for e in self.events()], ['3'])

    def test_actual_failover_success_is_one_request(self):
        proxy.USAGE = self.usage
        proxy.TL = proxy.EL = None
        server = serve(proxy.ProxyHandler)
        try:
            for protocol in ('openai', 'responses'):
                Receiver.protocol = protocol
                proxy.CONFIG = proxy.Config({'port': 0, 'models': [
                    {'name': 'primary', 'upstream_id': 'bad', 'protocol': protocol,
                     'base_url': self.base, 'api_key': 'SECRET', 'slot': '1', 'candidates': ['backup']},
                    {'name': 'backup', 'upstream_id': 'good', 'protocol': protocol,
                     'base_url': self.base, 'api_key': 'SECRET', 'slot': '2'}]})
                response = requests.post(f'http://127.0.0.1:{server.server_port}/v1/messages',
                    json={'model': 'primary', 'stream': True, 'messages': [], 'max_tokens': 10}, timeout=5)
                response.close()
                events = self.events()
                self.assertEqual(events[-2]['eventName'], 'dcc_model_failover')
                self.assertEqual(events[-2]['properties']['reason'], 'upstream_429')
                props = events[-1]['properties']
                self.assertTrue(props['success'])
                self.assertEqual(props['model'], 'good')
                self.assertEqual(props['switchCount'], 1)
            self.assertEqual(len(self.events()), 4)
        finally:
            server.shutdown()
            server.server_close()
            proxy.USAGE = None

    def test_actual_proxy_protocols_success_truncation_and_http_failure(self):
        proxy.USAGE = self.usage
        proxy.TL = proxy.EL = None
        server = serve(proxy.ProxyHandler)
        try:
            for protocol in ('openai', 'responses', 'anthropic'):
                Receiver.protocol = protocol
                proxy.CONFIG = proxy.Config({'port': 0, 'models': [{
                    'name': 'mock', 'upstream_id': 'mock', 'protocol': protocol,
                    'base_url': self.base, 'api_key': 'SECRET', 'slot': '1'}]})
                for stream in (True, False):
                    for outcome in ('success', 'truncated', 'http_error'):
                        with self.subTest(protocol=protocol, stream=stream, outcome=outcome):
                            Receiver.incomplete = outcome == 'truncated'
                            Receiver.upstream_status = 429 if outcome == 'http_error' else 200
                            before = len(self.events()) if self.usage.path.exists() else 0
                            with patch.object(proxy, 'compute_backoff', return_value=0):
                                response = requests.post(f'http://127.0.0.1:{server.server_port}/v1/messages',
                                    json={'model': 'mock', 'stream': stream, 'max_tokens': 10,
                                          'messages': [{'role': 'user', 'content': 'PRIVATE'}]}, timeout=5)
                            response.close()
                            # Content-Length permits the client to finish before handler finally runs.
                            deadline = time.monotonic() + 2
                            events = self.events()
                            while len(events) < before + 1 and time.monotonic() < deadline:
                                time.sleep(0.01)
                                events = self.events()
                            self.assertEqual(len(events), before + 1)
                            props = events[-1]['properties']
                            self.assertEqual(props['success'], outcome == 'success')
                            if outcome == 'success':
                                self.assertEqual(props['inputTokens'], 10)
                                self.assertEqual(props['outputTokens'], 2)
                            if outcome == 'http_error':
                                self.assertEqual(props['failReason'], 'upstream_429')
                            self.assertNotIn('PRIVATE', json.dumps(events))
                            self.assertNotIn('SECRET', json.dumps(events))
        finally:
            server.shutdown()
            server.server_close()
            proxy.USAGE = None


if __name__ == '__main__':
    unittest.main()
