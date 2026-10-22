import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import requests

from src.token_usage import RequestUsage, UsageStore
from tests.test_telemetry import Receiver, proxy, serve


class TokenUsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = UsageStore(self.root / 'token_usage.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def test_three_protocols_keep_last_usage_per_attempt_and_real_model(self):
        for protocol, events in {
            'openai': [
                {'usage': {'prompt_tokens': 8, 'completion_tokens': 2}},
                {'usage': {'prompt_tokens': 10, 'completion_tokens': 3,
                           'prompt_tokens_details': {'cached_tokens': 4}}}],
            'responses': [{'type': 'response.completed', 'response': {
                'usage': {'input_tokens': 6, 'output_tokens': 4}}}],
            'anthropic': [
                {'type': 'message_start', 'message': {'usage': {'input_tokens': 5,
                    'cache_creation_input_tokens': 2, 'cache_read_input_tokens': 3}}},
                {'type': 'message_delta', 'usage': {'output_tokens': 7}}],
        }.items():
            collector = RequestUsage(self.store)
            collector.observe('cc_in', {'req_id': protocol, 'slot': '1', 'protocol': protocol})
            collector.observe('gw_req', {'upstream_id': 'primary', 'protocol': protocol})
            collector.observe('gw_req', {'upstream_id': 'backup', 'protocol': protocol})
            for event in events:
                collector.observe('gw_chunk', {'line': 'data: ' + json.dumps(event)})
        rows = self.store.rows()
        self.assertEqual(len(rows), 6)
        self.assertEqual({r['model'] for r in rows}, {'primary', 'backup'})
        self.assertEqual([(r['input_tokens'], r['output_tokens']) for r in rows if r['model'] == 'backup'],
                         [(10, 3), (6, 4), (10, 7)])
        result = self.store.summary(7)
        self.assertEqual(result['total']['input_tokens'], 26)
        self.assertEqual(result['total']['output_tokens'], 14)
        self.assertEqual(result['total']['attempts'], 6)
        self.assertEqual(result['total']['unknown'], 3)

    def test_old_logs_import_incrementally_and_dedupe_live_rows(self):
        log = self.root / 'log'
        log.mkdir()
        day = datetime.now().strftime('%Y%m%d')
        first = log / f'{day}.jsonl'
        now = datetime.now().isoformat(timespec='milliseconds')
        def event(kind, **fields):
            return json.dumps({'ts': now, 'kind': kind, 'req_id': 'same', **fields}) + '\n'
        first.write_text(event('cc_in', slot='1', protocol='openai') +
                         event('gw_req', upstream_id='first', protocol='openai') +
                         event('gw_chunk', line='data: {"usage":{"prompt_tokens":2,"completion_tokens":1}}') +
                         '{bad json}\n' +
                         event('gw_req', upstream_id='second', protocol='openai'))
        self.store.import_logs(log)
        self.assertEqual(len(self.store.rows()), 2)
        self.assertEqual(self.store.summary(7)['total']['unknown'], 1)
        # The next chunk may be in a rotated file; replaying both files is idempotent.
        (log / f'{day}.1.jsonl').write_text(event('gw_chunk', line='data: {"usage":{"prompt_tokens":4,"completion_tokens":3}}'))
        self.store.import_logs(log)
        self.store.import_logs(log)
        self.assertEqual(len(self.store.rows()), 2)
        self.assertEqual(self.store.summary(7)['total']['input_tokens'], 6)
        with (log / f'{day}.1.jsonl').open('a') as stream:
            stream.write(event('gw_req', upstream_id='third', protocol='openai'))
        self.store.import_logs(log)
        self.assertEqual(len(self.store.rows()), 3)
        collector = RequestUsage(self.store)
        collector.observe('cc_in', {'req_id': 'same', 'slot': '1', 'protocol': 'openai'})
        collector.observe('gw_req', {'upstream_id': 'first'})
        collector.observe('gw_chunk', {'line': 'data: {"usage":{"prompt_tokens":2,"completion_tokens":1}}'})
        self.assertEqual(len(self.store.rows()), 3)

    def test_retention_and_no_body_or_key_in_database(self):
        old = datetime.now() - timedelta(days=91)
        self.store.record('old:1', 'old', 1, int(old.timestamp()*1000), 'x', '1', 'openai', 9, 9)
        collector = RequestUsage(self.store)
        collector.observe('cc_in', {'req_id': 'new', 'slot': '2', 'protocol': 'openai',
                                    'body': 'PRIVATE PROMPT', 'api_key': 'PRIVATE KEY'})
        collector.observe('gw_req', {'upstream_id': 'new-model', 'payload': 'PRIVATE PROMPT'})
        collector.observe('gw_chunk', {'line': 'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":1},"secret":"PRIVATE KEY"}'})
        self.assertEqual(len(self.store.rows()), 1)
        raw = (self.root / 'token_usage.sqlite3').read_bytes()
        self.assertNotIn(b'PRIVATE PROMPT', raw)
        self.assertNotIn(b'PRIVATE KEY', raw)
        self.assertEqual(self.store.summary(90)['total']['input_tokens'], 3)
        self.assertEqual(len(self.store.summary(7)['daily']), 7)

    def test_partial_usage_is_not_presented_as_complete(self):
        collector = RequestUsage(self.store)
        collector.observe('cc_in', {'req_id': 'partial', 'slot': '1', 'protocol': 'anthropic'})
        collector.observe('gw_req', {'upstream_id': 'm'})
        collector.observe('gw_chunk', {'line': 'data: {"type":"message_start","message":{"usage":{"input_tokens":8}}}'})
        summary = self.store.summary(7)
        self.assertEqual(summary['total']['input_tokens'], 8)
        self.assertEqual(summary['total']['unknown'], 1)
        self.assertEqual(summary['total']['measured'], 0)
        with self.assertRaises(ValueError):
            self.store.summary(8)

    def test_actual_attempts_count_retries_and_failover_once_each(self):
        collector = RequestUsage(self.store)
        collector.observe('cc_in', {'req_id': 'retry', 'slot': '1', 'protocol': 'openai'})
        collector.observe('gw_req', {'upstream_id': 'primary'})
        collector.observe('gw_attempt', {'upstream_id': 'primary'})
        collector.observe('gw_attempt', {'upstream_id': 'primary'})
        collector.observe('gw_req', {'upstream_id': 'backup'})
        collector.observe('gw_attempt', {'upstream_id': 'backup'})
        collector.observe('gw_chunk', {'line': 'data: {"usage":{"prompt_tokens":9,"completion_tokens":2}}'})
        self.assertEqual([(r['model'], r['input_tokens']) for r in self.store.rows()],
                         [('primary', None), ('primary', None), ('backup', 9)])
        self.assertEqual(self.store.summary(7)['total']['attempts'], 3)

    def test_import_new_attempt_events_matches_live_identity(self):
        log = self.root / 'log'; log.mkdir()
        day = datetime.now().strftime('%Y%m%d')
        now = datetime.now().isoformat(timespec='milliseconds')
        def event(kind, **fields):
            return json.dumps({'ts': now, 'kind': kind, 'req_id': 'retry', **fields}) + '\n'
        (log / f'{day}.jsonl').write_text(
            event('cc_in', slot='1', protocol='openai') +
            event('gw_req', upstream_id='primary', protocol='openai') +
            event('gw_attempt', upstream_id='primary', protocol='openai') +
            event('gw_attempt', upstream_id='primary', protocol='openai') +
            event('gw_req', upstream_id='backup', protocol='openai') +
            event('gw_attempt', upstream_id='backup', protocol='openai') +
            event('gw_chunk', line='data: {"usage":{"prompt_tokens":9,"completion_tokens":2}}'))
        self.store.import_logs(log)
        self.assertEqual([(r['model'], r['input_tokens']) for r in self.store.rows()],
                         [('primary', None), ('primary', None), ('backup', 9)])
        self.store.import_logs(log)
        self.assertEqual(len(self.store.rows()), 3)

    def test_import_waits_for_complete_log_line(self):
        log = self.root / 'log'; log.mkdir()
        path = log / f'{datetime.now():%Y%m%d}.jsonl'
        line = json.dumps({'ts': datetime.now().isoformat(timespec='milliseconds'),
                           'kind': 'gw_req', 'req_id': 'pending',
                           'upstream_id': 'model', 'protocol': 'openai'})
        path.write_text(line)
        self.store.import_logs(log)
        self.assertEqual(len(self.store.rows()), 0)
        with path.open('a') as stream:
            stream.write('\n')
        self.store.import_logs(log)
        self.assertEqual(len(self.store.rows()), 1)

    def test_proxy_collects_all_protocols_when_telemetry_is_disabled(self):
        upstream = serve(Receiver)
        receiver = serve(proxy.ProxyHandler)
        Receiver.incomplete = False
        Receiver.upstream_status = 200
        try:
            with patch.object(proxy, 'STATS', self.store), \
                 patch.object(proxy, 'USAGE', None), \
                 patch.object(proxy, 'TL', None):
                for protocol in ('openai', 'responses', 'anthropic'):
                    Receiver.protocol = protocol
                    proxy.CONFIG = proxy.Config({'port': 0, 'models': [{
                        'name': 'local', 'upstream_id': f'upstream-{protocol}',
                        'protocol': protocol, 'base_url': f'http://127.0.0.1:{upstream.server_port}',
                        'api_key': 'SECRET', 'slot': '1'}]})
                    for stream in (True, False):
                        response = requests.post(f'http://127.0.0.1:{receiver.server_port}/v1/messages',
                            json={'model': 'local', 'messages': [], 'max_tokens': 10, 'stream': stream},
                            timeout=5)
                        self.assertEqual(response.status_code, 200)
                        response.close()
                deadline = time.monotonic() + 2
                while len(self.store.rows()) < 6 and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(len(self.store.rows()), 6)
                self.assertEqual(self.store.summary(7)['total']['input_tokens'], 60)
                self.assertEqual(self.store.summary(7)['total']['output_tokens'], 12)
        finally:
            receiver.shutdown()
            receiver.server_close()
            upstream.shutdown()
            upstream.server_close()

    def test_proxy_counts_internal_retries_as_separate_unknown_attempts(self):
        upstream = serve(Receiver)
        receiver = serve(proxy.ProxyHandler)
        Receiver.upstream_status = 200
        Receiver.incomplete = False
        try:
            with patch.object(proxy, 'STATS', self.store), \
                 patch.object(proxy, 'USAGE', None), \
                 patch.object(proxy, 'TL', None), \
                 patch.object(proxy, 'compute_backoff', return_value=0):
                for protocol in ('openai', 'responses', 'anthropic'):
                    Receiver.protocol = protocol
                    proxy.CONFIG = proxy.Config({'port': 0, 'models': [{
                        'name': 'local', 'upstream_id': 'bad', 'protocol': protocol,
                        'base_url': f'http://127.0.0.1:{upstream.server_port}',
                        'api_key': 'SECRET', 'slot': '1'}]})
                    response = requests.post(f'http://127.0.0.1:{receiver.server_port}/v1/messages',
                        json={'model': 'local', 'messages': [], 'max_tokens': 10, 'stream': False},
                        timeout=5)
                    self.assertEqual(response.status_code, 429)
                    response.close()
                expected = 3 * (proxy.MAX_RETRIES + 1)
                deadline = time.monotonic() + 2
                while len(self.store.rows()) < expected and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(len(self.store.rows()), expected)
                self.assertEqual(self.store.summary(7)['total']['unknown'], expected)
        finally:
            receiver.shutdown()
            receiver.server_close()
            upstream.shutdown()
            upstream.server_close()

    def test_proxy_failover_attributes_usage_to_backup(self):
        upstream = serve(Receiver)
        receiver = serve(proxy.ProxyHandler)
        Receiver.upstream_status = 200
        Receiver.incomplete = False
        try:
            with patch.object(proxy, 'STATS', self.store), \
                 patch.object(proxy, 'USAGE', None), \
                 patch.object(proxy, 'TL', None):
                for protocol in ('openai', 'responses'):
                    Receiver.protocol = protocol
                    proxy.CONFIG = proxy.Config({'port': 0, 'models': [
                        {'name': 'primary', 'upstream_id': 'bad', 'protocol': protocol,
                         'base_url': f'http://127.0.0.1:{upstream.server_port}',
                         'api_key': 'SECRET', 'slot': '1', 'candidates': ['backup']},
                        {'name': 'backup', 'upstream_id': 'good', 'protocol': protocol,
                         'base_url': f'http://127.0.0.1:{upstream.server_port}',
                         'api_key': 'SECRET', 'slot': '2'}]})
                    response = requests.post(f'http://127.0.0.1:{receiver.server_port}/v1/messages',
                        json={'model': 'primary', 'messages': [], 'max_tokens': 10, 'stream': True},
                        timeout=5)
                    self.assertEqual(response.status_code, 200)
                    response.close()
                rows = self.store.rows()
                self.assertEqual([(r['model'], r['input_tokens']) for r in rows],
                                 [('bad', None), ('good', 10), ('bad', None), ('good', 10)])
        finally:
            receiver.shutdown()
            receiver.server_close()
            upstream.shutdown()
            upstream.server_close()
