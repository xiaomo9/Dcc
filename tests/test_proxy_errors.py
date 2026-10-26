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
    models = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        type(self).last_body = body
        type(self).calls += 1
        type(self).models.append(body['model'])
        outcome = 'success' if body['model'] == 'other' else self.outcome
        status = {'bad_request': 400, 'limited': 429}.get(outcome, 200)
        self.send_response(status)
        if outcome == 'broken':
            self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()
        if status != 200:
            self.wfile.write(b'{"error":"upstream rejected request"}')
            return
        if outcome == 'empty':
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
        if outcome == 'success':
            events.append(finish)
        elif outcome == 'event_error':
            events.append(error)
        elif outcome == 'event_error_before_text':
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

    def test_vscode_unknown_model_is_rejected_but_cli_fallback_still_works(self):
        self.configure('openai')
        Upstream.outcome = 'success'
        for endpoint in ('messages', 'messages/count_tokens'):
            for stream in (False, True):
                url = f'http://127.0.0.1:{self.server.server_port}/v1/{endpoint}'
                body = {'model': 'missing', 'messages': [], 'max_tokens': 20, 'stream': stream}
                response = self.client.post(url, json=body, headers={'x-dcc-client': 'vscode'})
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json()['error']['type'], 'not_found_error')
                self.assertIn('missing', response.json()['error']['message'])
                response = self.client.post(url, json=body)
                self.assertEqual(response.status_code, 200)
                body['model'] = 'mock[1m]'
                response = self.client.post(url, json=body, headers={'x-dcc-client': 'vscode'})
                self.assertEqual(response.status_code, 200)

    def test_vscode_slot_alias_reaches_its_model_instead_of_default(self):
        self.configure('openai')
        Upstream.outcome = 'success'
        primary = proxy.CONFIG.models_by_name['mock']
        primary['slot'] = '1'
        other = {**primary, 'name': 'other', 'slot': '2', 'upstream_id': 'other'}
        proxy.CONFIG = proxy.Config({'port': 0, 'models': [primary, other]})
        response = self.client.post(
            f'http://127.0.0.1:{self.server.server_port}/v1/messages',
            json={'model': 'dcc-slot-2[1m]', 'messages': [], 'max_tokens': 20},
            headers={'x-dcc-client': 'vscode'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Upstream.models[-1], 'other')

    def configure(self, protocol, guarded=False):
        model = {'name': 'mock', 'protocol': protocol, 'upstream_id': 'mock',
                 'base_url': f'http://127.0.0.1:{self.upstream.server_port}', 'api_key': 'test'}
        models = [model]
        if guarded:
            model['candidates'] = ['other']
            models.append({**model, 'name': 'other', 'upstream_id': 'other', 'candidates': []})
        proxy.CONFIG = proxy.Config({'port': 0, 'models': models})
        Upstream.protocol, Upstream.calls, Upstream.models = protocol, 0, []

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

    def test_image_capability_guard(self):
        image = {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'test'}}
        for protocol in ('openai', 'responses', 'anthropic'):
            for stream in (False, True):
                for content in ([image], [{'type': 'image_url', 'image_url': {'url': 'https://invalid.invalid/a.png'}}],
                                [{'type': 'tool_result', 'tool_use_id': 'call_test', 'content': [image]}]):
                    for scenario in ('new', 'history', 'supported'):
                        with self.subTest(protocol=protocol, stream=stream, content=content, scenario=scenario):
                            self.configure(protocol)
                            Upstream.outcome = 'success'
                            proxy.CONFIG.models_by_name['mock']['supports_images'] = scenario == 'supported'
                            messages = [{'role': 'user', 'content': content}]
                            if scenario == 'history':
                                messages += [{'role': 'assistant', 'content': 'previous answer'},
                                             {'role': 'user', 'content': 'continue'}]
                            response = self.client.post(
                                f'http://127.0.0.1:{self.server.server_port}/v1/messages',
                                json={'model': 'mock', 'stream': stream, 'messages': messages}, timeout=5)
                            if scenario == 'new':
                                self.assertEqual(response.status_code, 400)
                                self.assertEqual(response.json()['error']['type'], 'invalid_request_error')
                                self.assertIn('请切换到支持图片的模型', response.json()['error']['message'])
                                self.assertEqual(Upstream.calls, 0)
                            else:
                                self.assertEqual(response.status_code, 200)
                                self.assertEqual(Upstream.calls, 1)
                                if scenario == 'history':
                                    self.assertEqual(response.headers['x-dcc-warn-images-stripped'], '1')
                                    payload = json.dumps(Upstream.last_body)
                                    self.assertNotIn('"type": "image"', payload)
                                    self.assertNotIn('"type": "image_url"', payload)
                                    self.assertNotIn('"type": "input_image"', payload)
                                elif protocol == 'anthropic':
                                    self.assertEqual(Upstream.last_body['messages'], messages)
                                if stream:
                                    text = ''.join(event.get('delta', {}).get('text', '')
                                                   for _, event in proxy._parse_sse_events(response.content))
                                else:
                                    text = ''.join(block.get('text', '') for block in response.json()['content'])
                                self.assertEqual(text, 'partial text')

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

    def test_guarded_http_errors_respect_switch_setting(self):
        for protocol in ('openai', 'responses'):
            for stream in (True, False):
                for outcome, enabled, expected_models, status in (
                    ('bad_request', True, ['mock'], 400),
                    ('limited', False, ['mock'] * (1 + proxy.MAX_RETRIES), 429),
                    ('limited', True, ['mock', 'other'], 200),
                ):
                    with self.subTest(protocol=protocol, stream=stream,
                                      outcome=outcome, enabled=enabled):
                        self.configure(protocol, guarded=True)
                        proxy.CONFIG.switch_on_429 = enabled
                        Upstream.outcome = outcome
                        with patch.object(proxy, 'compute_backoff', return_value=0):
                            response = self.request(stream)
                        self.assertEqual(Upstream.models, expected_models)
                        if status != 200:
                            self.assert_failure(response, stream, status)
                        else:
                            self.assertEqual(response.status_code, 200)
