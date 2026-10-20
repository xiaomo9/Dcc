import http.server
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import proxy_server as proxy
from test_telemetry import serve


class Upstream(http.server.BaseHTTPRequestHandler):
    protocol = 'openai'
    outcome = 'success'
    calls = 0

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        type(self).calls += 1
        status = {'bad_request': 400, 'limited': 429}.get(self.outcome, 200)
        self.send_response(status)
        if self.outcome == 'broken':
            self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()
        if status != 200:
            self.wfile.write(b'{"error":"upstream rejected request"}')
            return
        if self.outcome == 'empty':
            return
        if self.protocol == 'openai':
            events = [{'choices': [{'delta': {'content': 'partial text'}, 'finish_reason': None}]}]
            finish = {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}
            error = {'error': {'message': 'failed event'}}
        elif self.protocol == 'responses':
            events = [{'type': 'response.output_text.delta', 'delta': 'partial text'}]
            finish = {'type': 'response.completed', 'response': {}}
            error = {'type': 'response.failed', 'response': {'error': {'message': 'failed event'}}}
        else:
            events = [
                {'type': 'message_start', 'message': {'id': 'm', 'content': [], 'model': 'mock'}},
                {'type': 'content_block_start', 'index': 0,
                 'content_block': {'type': 'text', 'text': ''}},
                {'type': 'content_block_delta', 'index': 0,
                 'delta': {'type': 'text_delta', 'text': 'partial text'}}]
            finish = {'type': 'message_stop'}
            error = {'type': 'error', 'error': {'type': 'api_error', 'message': 'failed event'}}
        if self.outcome == 'success':
            events.append(finish)
        elif self.outcome == 'event_error':
            events.append(error)
        elif self.outcome == 'event_error_before_text':
            events = [error]
        for event in events:
            frame = ('event: ' + event.get('type', 'chunk') + '\ndata: ' +
                     json.dumps(event) + '\n\n').encode()
            if self.outcome == 'broken':
                frame = f'{len(frame):x}\r\n'.encode() + frame + b'\r\n'
            self.wfile.write(frame)
            self.wfile.flush()
        if self.outcome == 'timeout':
            # Fill requests' native-stream read buffer so complete text frames reach
            # the proxy before the idle timeout, rather than testing pre-frame retry.
            self.wfile.write(b':' + b' ' * 1024 + b'\n\n')
            self.wfile.flush()
            time.sleep(0.15)
        # broken: omit the terminating HTTP chunk, simulating a lost connection.


class ProxyErrorTests(unittest.TestCase):
    def setUp(self):
        self.upstream = serve(Upstream)
        self.server = serve(proxy.ProxyHandler)
        self.client = requests.Session()
        self.client.trust_env = False
        self.old_config = getattr(proxy, 'CONFIG', None)
        self.logging = patch.object(proxy, '_log')
        self.logging.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.client.close()
        proxy.CONFIG = self.old_config
        self.logging.stop()

    def configure(self, protocol, guarded=False):
        model = {'name': 'mock', 'protocol': protocol, 'upstream_id': 'mock',
                 'base_url': f'http://127.0.0.1:{self.upstream.server_port}', 'api_key': 'test'}
        models = [model]
        if guarded:
            model['candidates'] = ['other']
            models.append({**model, 'name': 'other', 'upstream_id': 'other', 'candidates': []})
        proxy.CONFIG = proxy.Config({'port': 0, 'models': models})
        Upstream.protocol, Upstream.calls = protocol, 0

    def request(self, stream):
        return self.client.post(f'http://127.0.0.1:{self.server.server_port}/v1/messages',
                                json={'model': 'mock', 'stream': stream, 'messages': []}, timeout=5)

    def assert_failure(self, response, stream, status, partial=False):
        if not stream:
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.json()['type'], 'error')
            self.assertNotIn('content', response.json())
            return
        self.assertEqual(response.status_code, 200)
        events = list(proxy._parse_sse_events(response.content))
        names = [name for name, _ in events]
        self.assertEqual(names.count('error'), 1)
        self.assertEqual(names[-1], 'error')
        self.assertNotIn('message_stop', names)
        self.assertFalse(any(event.get('delta', {}).get('stop_reason') for _, event in events))
        text = ''.join(event.get('delta', {}).get('text', '') for _, event in events)
        self.assertNotIn('failed event', text)
        self.assertNotIn('[dcc-proxy]', text)
        if partial:
            self.assertLessEqual(text.count('partial text'), 1)

    def test_http_and_stream_failures(self):
        outcomes = {'bad_request': 400, 'limited': 429, 'empty': 502, 'truncated': 502,
                    'event_error': 502, 'event_error_before_text': 502,
                    'broken': 502, 'timeout': 504}
        for protocol in ('openai', 'responses', 'anthropic'):
            for stream in (True, False):
                for outcome, status in outcomes.items():
                    with self.subTest(protocol=protocol, stream=stream, outcome=outcome):
                        self.configure(protocol)
                        Upstream.outcome = outcome
                        with patch.object(proxy, 'compute_backoff', return_value=0), \
                                patch.object(proxy, 'STREAM_IDLE_TIMEOUT', 0.05):
                            response = self.request(stream)
                        self.assert_failure(response, stream, status, partial=True)
                        self.assertEqual(Upstream.calls, 4 if outcome == 'limited' else 1)

    def test_connection_failure_and_timeout_before_first_frame(self):
        for protocol in ('openai', 'responses', 'anthropic'):
            for stream in (True, False):
                for error, status in ((requests.ConnectionError('refused'), 502),
                                      (requests.ReadTimeout('timeout'), 504)):
                    with self.subTest(protocol=protocol, stream=stream, status=status):
                        self.configure(protocol)
                        # Only the proxy calls requests.post; the client uses its own Session.
                        with patch.object(proxy.requests, 'post', side_effect=error) as upstream:
                            response = self.request(stream)
                        self.assert_failure(response, stream, status)
                        self.assertEqual(upstream.call_count, 1 + proxy.STREAM_MAX_RETRIES)

    def test_guarded_partial_stream_failure_does_not_switch_or_replay(self):
        for protocol in ('openai', 'responses'):
            for stream in (True, False):
                with self.subTest(protocol=protocol, stream=stream):
                    self.configure(protocol, guarded=True)
                    Upstream.outcome = 'broken'
                    response = self.request(stream)
                    self.assert_failure(response, stream, 502, partial=True)
                    self.assertEqual(Upstream.calls, 1)

    def test_guarded_success_stays_on_first_candidate(self):
        for protocol in ('openai', 'responses'):
            for stream in (True, False):
                with self.subTest(protocol=protocol, stream=stream):
                    self.configure(protocol, guarded=True)
                    Upstream.outcome = 'success'
                    response = self.request(stream)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(Upstream.calls, 1)
                    if stream:
                        names = [name for name, _ in proxy._parse_sse_events(response.content)]
                        self.assertNotIn('error', names)
                        self.assertEqual(names.count('message_stop'), 1)
                    else:
                        self.assertEqual(response.json()['content'], [
                            {'type': 'text', 'text': 'partial text'}])
