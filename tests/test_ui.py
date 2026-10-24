import json
import http.server
from contextlib import contextmanager
import socket
import stat
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, call, patch

from src import cli, ui


class ConfigUITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dcc = self.root / 'dcc.ini'
        self.llm = self.root / 'llm.ini'
        self.settings = self.root / '.claude/settings.json'
        self.dcc.write_text(
            '[proxy]\nport = 4000\nllm_ini_path = ' + str(self.llm) + '\n'
            '[output]\ndir = ' + str(self.root / 'output') + '\n'
            '[models]\n# keep slot note\n2 = first\n1 = second\n'
            '[model_names]\n2 = Alpha\n1 = Beta\n'
            '[context]\n[groups]\nA = 2,1\n'
            '[failover]\nsoft_timeout = 12\nswitch_on_429 = true\nmax_candidates = 2\n')
        self.llm.write_text(
            '# keep file note\n[first]\nbase_url = http://one.invalid/v1\n'
            'api_key = secret-one\nmodel = upstream-one\nprotocol = openai\n'
            'custom = original\n\n[second]\nbase_url = http://two.invalid/v1\n'
            'api_key = secret-two\nmodel = upstream-two\nprotocol = responses\n')
        self.editor = ui.ConfigEditor(self.dcc, self.settings)

    def test_update_endpoint_requires_auth_and_does_not_change_configuration(self):
        from src.updates import ReleaseChecker
        before = (self.dcc.read_bytes(), self.llm.read_bytes())
        with patch.object(ReleaseChecker, 'check', return_value={'available': False}) as check:
            with self.runtime_server() as request:
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    request(headers={}, path='/api/update')
                self.assertEqual(rejected.exception.code, 403)
                check.assert_not_called()
                self.assertEqual(request(path='/api/update'), {'available': False})
                check.assert_called_once()
        self.assertEqual(before, (self.dcc.read_bytes(), self.llm.read_bytes()))
        self.assertFalse(self.settings.exists())

    def tearDown(self):
        self.tmp.cleanup()

    @contextmanager
    def runtime_server(self):
        server, token = ui.create_server(self.editor, token='test-token')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(data=None, headers=None, path='/api/runtime'):
            req = urllib.request.Request(
                f'http://127.0.0.1:{server.server_port}' + path,
                data=None if data is None else json.dumps(data).encode(),
                headers={'X-DCC-Token': token} if headers is None else headers)
            with urllib.request.urlopen(req, timeout=10) as response:
                return json.load(response)

        try:
            yield request
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_settings_http_syncs_disk_config_and_preserves_draft(self):
        self.settings.parent.mkdir()
        original = b'{"theme":"dark"}\n'
        self.settings.write_bytes(original)
        draft = self.editor.state()
        draft['slots'].reverse()
        before = (self.dcc.read_bytes(), self.llm.read_bytes())
        with self.runtime_server() as request, \
             patch.object(ui.P, 'start') as start, \
             patch.object(ui.P, 'restart') as restart, \
             patch.object(ui.CC, 'stop_all') as stop:
            result = request({}, path='/api/settings')
            self.assertTrue(result['changed'])
            self.assertEqual(set(result), {'changed', 'settings_revision'})
            self.assertEqual(list(self.settings.parent.iterdir()), [self.settings])
            synced = json.loads(self.settings.read_text())
            self.assertEqual(synced['model'], 'dcc-slot-2')
            self.assertEqual(synced['theme'], 'dark')
            self.assertNotIn('secret-one', json.dumps(result))
            self.assertEqual((self.dcc.read_bytes(), self.llm.read_bytes()), before)
            with self.assertRaisesRegex(ValueError, '外部修改'):
                self.editor.prepare(draft)
            draft['revisions']['settings'] = result['settings_revision']
            prepared = self.editor.prepare(draft)
            self.assertLess(prepared['dcc'].index('1 = second'), prepared['dcc'].index('2 = first'))
            files = list(self.settings.parent.iterdir())
            stamp = self.settings.stat().st_mtime_ns
            again = request({}, path='/api/settings')
            self.assertFalse(again['changed'])
            self.assertEqual(set(again), {'changed', 'settings_revision'})
            self.assertEqual(again['settings_revision'], result['settings_revision'])
            self.assertEqual(self.settings.stat().st_mtime_ns, stamp)
            self.assertCountEqual(list(self.settings.parent.iterdir()), files)
            start.assert_not_called()
            restart.assert_not_called()
            stop.assert_not_called()

    def test_settings_sync_waits_for_ui_lock(self):
        attempted, finished = threading.Event(), threading.Event()
        errors = []

        def sync():
            attempted.set()
            try:
                self.editor.sync_settings()
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()

        with patch.object(ui.S, 'write', wraps=ui.S.write) as write:
            thread = threading.Thread(target=sync)
            try:
                with ui._lock(self.editor.lock_path):
                    thread.start()
                    self.assertTrue(attempted.wait(2))
                    self.assertFalse(finished.wait(.1))
                    write.assert_not_called()
            finally:
                thread.join(timeout=4)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            write.assert_called_once()

    def test_settings_http_rejects_unauthorized_and_extra_parameters(self):
        with self.runtime_server() as request, patch.object(ui.S, 'write') as write:
            for headers in ({}, {'X-DCC-Token': 'test-token', 'Origin': 'http://evil.invalid'}):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request({}, headers, path='/api/settings')
                self.assertEqual(error.exception.code, 403)
            for data in ({'path': '/tmp/other-settings.json'}, [], 'settings'):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request(data, path='/api/settings')
                self.assertEqual(error.exception.code, 400)
            write.assert_not_called()

    def test_settings_http_masks_failure_and_preserves_invalid_file(self):
        self.settings.parent.mkdir()
        original = b'{secret-one'
        self.settings.write_bytes(original)
        with self.runtime_server() as request:
            with self.assertRaises(urllib.error.HTTPError) as error:
                request({}, path='/api/settings')
            self.assertEqual(error.exception.code, 400)
            message = json.load(error.exception)['error']
            self.assertIn('同步 Claude 配置失败', message)
            self.assertNotIn('secret-one', message)
            self.assertEqual(self.settings.read_bytes(), original)
            self.assertEqual(list(self.settings.parent.iterdir()), [self.settings])

    def test_runtime_actions_reuse_cli_order_and_preserve_config_files(self):
        self.settings.parent.mkdir()
        self.settings.write_text('{"theme":"dark"}\n')
        self.dcc.write_text(self.dcc.read_text().replace('port = 4000', 'port = 4321'))
        before = {path: path.read_bytes() for path in (self.dcc, self.llm, self.settings)}
        for action in ('start', 'stop', 'reset'):
            with self.subTest(action=action), \
                 patch.object(ui.P, 'start', return_value=123) as start, \
                 patch.object(ui.P, 'stop_proxy', return_value=False) as stop, \
                 patch.object(ui.P, 'restart', return_value=False) as restart, \
                 patch.object(ui.CC, 'stop_all', return_value=0) as stop_cc:
                calls = Mock()
                for name, method in (('start', start), ('stop', stop),
                                     ('restart', restart), ('stop_cc', stop_cc)):
                    calls.attach_mock(method, name)
                result = self.editor.runtime_action({'action': action})
                method = start if action == 'start' else stop if action == 'stop' else restart
                cfg = method.call_args.args[0]
                self.assertEqual(cfg.port, 4321)
                expected = ([call.start(cfg, fixed_port=True)] if action == 'start' else
                            [call.stop_cc(cfg), getattr(call, 'stop' if action == 'stop' else 'restart')(cfg)])
                self.assertEqual(calls.mock_calls, expected)
                self.assertEqual(result, {'stopped': 0, 'proxy_stopped': False})
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertFalse(list(self.root.rglob('*.dcc-backup-*')))

    def test_runtime_status_does_not_return_model_secrets(self):
        proxy = {'pid': 123, 'port': 4000, 'port_open': True, 'healthy': True}
        with patch.object(ui.P, 'status', return_value=proxy), \
             patch.object(ui.CC, 'list_instances', return_value={'123': {'pid': 123}}):
            result = self.editor.runtime_status()
        self.assertEqual(result, {'proxy': proxy, 'instances': 1})
        self.assertNotIn('secret-one', json.dumps(result))

    def test_runtime_status_rejects_other_healthy_services(self):
        class HealthService(http.server.BaseHTTPRequestHandler):
            body = b''

            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(self.body)

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), HealthService)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.dcc.write_text(self.dcc.read_text().replace('port = 4000', f'port = {server.server_port}'))
        try:
            with self.runtime_server() as request:
                for body, healthy in (
                    (b'{"status":"ok"}', False),
                    (b'{"status":"ok","service":"other","capabilities":["vscode"]}', False),
                    (b'not json', False),
                    (b'[]', False),
                    (b'{"status":"ok","service":"dcc-proxy","capabilities":["vscode"]}', True),
                ):
                    with self.subTest(body=body):
                        HealthService.body = body
                        proxy = request()['proxy']
                        self.assertTrue(proxy['port_open'])
                        self.assertIsNone(proxy['pid'])
                        self.assertEqual(proxy['healthy'], healthy)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        proxy = self.editor.runtime_status()['proxy']
        self.assertFalse(proxy['port_open'])
        self.assertFalse(proxy['healthy'])

    def test_runtime_waits_for_configuration_save(self):
        draft = self.editor.state()
        entered, release, attempted, started = (threading.Event() for _ in range(4))
        errors = []

        def prepare(value):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('save was not released')
            return {'llm': self.llm.read_text(), 'dcc': self.dcc.read_text()}

        def save():
            try:
                self.editor.save(draft)
            except BaseException as error:
                errors.append(error)

        def start():
            attempted.set()
            try:
                self.editor.runtime_action({'action': 'start'})
            except BaseException as error:
                errors.append(error)

        with patch.object(self.editor, 'prepare', side_effect=prepare), \
             patch.object(ui.P, 'start', side_effect=lambda *args, **kwargs: started.set()):
            save_thread = threading.Thread(target=save)
            start_thread = threading.Thread(target=start)
            save_thread.start()
            try:
                self.assertTrue(entered.wait(2))
                start_thread.start()
                self.assertTrue(attempted.wait(2))
                self.assertFalse(started.wait(.1), 'runtime bypassed the save lock')
            finally:
                release.set()
                save_thread.join(timeout=4)
                if start_thread.ident is not None:
                    start_thread.join(timeout=4)
            self.assertTrue(started.is_set())
            self.assertFalse(save_thread.is_alive())
            self.assertFalse(start_thread.is_alive())
            self.assertEqual(errors, [])

    def test_runtime_http_rejects_unauthorized_and_unknown_actions(self):
        with self.runtime_server() as request, \
             patch.object(ui.P, 'start') as start:
            for data in (None, {'action': 'start'}):
                for headers in ({}, {'X-DCC-Token': 'test-token', 'Origin': 'http://evil.invalid'},
                                {'X-DCC-Token': 'test-token', 'Host': 'evil.invalid'}):
                    with self.subTest(data=data, headers=headers), \
                         self.assertRaises(urllib.error.HTTPError) as error:
                        request(data, headers)
                    self.assertEqual(error.exception.code, 403)
            for data in ({'action': 'shell'}, {'action': []}, {}, [], 'start'):
                with self.subTest(data=data), self.assertRaises(urllib.error.HTTPError) as error:
                    request(data)
                self.assertEqual(error.exception.code, 400)
            start.assert_not_called()

    def test_runtime_http_masks_failures_and_can_refresh_after_failure(self):
        proxy = {'pid': None, 'port': 4000, 'port_open': False, 'healthy': False}
        with self.runtime_server() as request, \
             patch.object(ui.P, 'status', return_value=proxy):
            for failure in (SystemExit(5), RuntimeError('secret-one'), ValueError('secret-one')):
                with self.subTest(failure=failure), \
                     patch.object(ui.P, 'start', side_effect=failure), \
                     self.assertRaises(urllib.error.HTTPError) as error:
                    request({'action': 'start'})
                message = json.load(error.exception)['error']
                self.assertEqual(error.exception.code, 400)
                self.assertIn('启动代理失败', message)
                self.assertNotIn('secret-one', message)
                self.assertEqual(request()['proxy'], proxy)
            with patch.object(ui, 'load_config', side_effect=ValueError('secret-one')):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request()
                self.assertNotIn('secret-one', error.exception.read().decode())
                self.assertEqual(error.exception.code, 400)

    def test_runtime_http_controls_real_isolated_proxy_and_keeps_ui_running(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.dcc.write_text(self.dcc.read_text().replace('port = 4000', f'port = {port}'))
        cfg = ui.load_config(self.dcc)
        before = (self.dcc.read_bytes(), self.llm.read_bytes())
        try:
            with self.runtime_server() as request:
                self.assertFalse(request()['proxy']['healthy'])
                request({'action': 'start'})
                initial = request()
                self.assertTrue(initial['proxy']['healthy'])
                self.assertEqual(initial['proxy']['port'], port)
                self.assertEqual(initial['instances'], 0)
                request({'action': 'start'})
                self.assertEqual(request()['proxy']['pid'], initial['proxy']['pid'])
                request({'action': 'reset'})
                restarted = request()['proxy']
                self.assertTrue(restarted['healthy'])
                self.assertNotEqual(restarted['pid'], initial['proxy']['pid'])
                request({'action': 'stop'})
                stopped = request()['proxy']
                self.assertFalse(stopped['healthy'])
                self.assertIsNone(stopped['pid'])
                self.assertEqual(request({'action': 'stop'})['stopped'], 0)
                self.assertEqual((self.dcc.read_bytes(), self.llm.read_bytes()), before)
                self.assertFalse(self.settings.exists())
                with socket.socket() as occupied:
                    occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    occupied.bind(('127.0.0.1', port))
                    occupied.listen()
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        request({'action': 'start'})
                    self.assertEqual(error.exception.code, 400)
                    self.assertIn('启动代理失败', json.load(error.exception)['error'])
                    self.assertFalse(cfg.proxy_pidfile.exists())
        finally:
            ui.P.stop_proxy(cfg)

    def test_ui_command_uses_foreground_panel_without_loading_model_config(self):
        with patch.object(ui, 'run') as run:
            self.assertEqual(cli.main(['ui']), 0)
            run.assert_called_once()
            self.assertNotEqual(cli.main(['ui', 'extra']), 0)

    def test_read_masks_keys_and_reveal_is_explicit(self):
        state = self.editor.state()
        self.assertNotIn('secret-one', json.dumps(state))
        self.assertNotIn('suffix', json.dumps(state['slots']))
        self.assertNotIn('模型后缀', (Path(ui.__file__).with_name('ui.html')).read_text())
        self.assertTrue(state['models'][0]['has_key'])
        self.assertEqual(self.editor.reveal_key('first'), 'secret-one')

    def test_section_name_is_not_saved_as_model_option(self):
        self.llm.write_text(self.llm.read_text().replace('\n[second]', '\nsection = first\n\n[second]'))
        state = self.editor.state()
        self.assertNotIn('section', state['models'][0]['fields'])
        self.assertNotIn('section = first', self.editor.prepare(state)['llm'])
        state['models'][0]['fields']['section'] = 'first'
        with self.assertRaisesRegex(ValueError, '模型扩展字段无效'):
            self.editor.prepare(state)

    def test_image_capability_is_written_even_when_default_is_false(self):
        state = self.editor.state()
        self.assertNotIn('supports_images', state['models'][0]['fields'])
        prepared = ui._parser(self.editor.prepare(state)['llm'].encode())
        self.assertEqual(prepared.get('first', 'supports_images'), 'false')
        state['models'][0]['fields']['supports_images'] = 'true'
        prepared = ui._parser(self.editor.prepare(state)['llm'].encode())
        self.assertEqual(prepared.get('first', 'supports_images'), 'true')
        state['models'][0]['fields']['supports_images'] = 'false'
        prepared = ui._parser(self.editor.prepare(state)['llm'].encode())
        self.assertEqual(prepared.get('first', 'supports_images'), 'false')

    def test_edit_preserves_comments_reorders_slots_and_renames_reference(self):
        state = self.editor.state()
        models = state['models']
        models[0]['section'] = 'renamed'
        models[0]['fields']['custom'] = 'updated'
        slots = state['slots']
        slots[0]['section'] = 'renamed'
        state['slots'] = [slots[1], slots[0]]
        prepared = self.editor.prepare(state)
        self.assertIn('# keep file note', prepared['llm'])
        self.assertIn('[renamed]', prepared['llm'])
        self.assertIn('custom = updated', prepared['llm'])
        self.assertIn('api_key = secret-one', prepared['llm'])
        self.assertIn('# keep slot note', prepared['dcc'])
        self.assertLess(prepared['dcc'].index('1 = second'), prepared['dcc'].index('2 = renamed'))

    def test_referenced_delete_and_external_change_are_rejected(self):
        state = self.editor.state()
        state['models'] = [m for m in state['models'] if m['section'] != 'first']
        with self.assertRaisesRegex(ValueError, '引用'):
            self.editor.prepare(state)
        state = self.editor.state()
        self.llm.write_text(self.llm.read_text() + '\n# external edit\n')
        with self.assertRaisesRegex(ValueError, '外部修改'):
            self.editor.prepare(state)

    def test_renaming_to_a_deleted_section_does_not_duplicate_it(self):
        state = self.editor.state()
        state['models'][0]['section'] = 'second'
        state['models'].pop()
        state['slots'][0]['section'] = 'second'
        state['slots'].pop()
        state['groups'][0]['members'] = ['2']
        prepared = self.editor.prepare(state)
        self.assertEqual(prepared['llm'].count('[second]'), 1)
        self.assertIn('upstream-one', prepared['llm'])
        self.assertNotIn('upstream-two', prepared['llm'])

    def test_clearing_slot_overrides_removes_keys_and_restores_defaults(self):
        self.dcc.write_text(self.dcc.read_text()
                            .replace('1 = Beta\n', '1 = \n')
                            .replace('[context]\n', '[model_suffix]\n1 = [2m]\n[context]\n')
                            .replace('[context]\n', '[context]\n1 = 500k\n'))
        state = self.editor.state()
        slot = next(s for s in state['slots'] if s['id'] == '1')
        slot['context'] = ''
        prepared = self.editor.prepare(state)
        config = ui._parser(prepared['dcc'].encode())
        for section in ('model_names', 'context'):
            self.assertFalse(config.has_option(section, '1'), section)
        self.assertFalse(config.has_section('model_suffix'))
        self.dcc.write_text(prepared['dcc'])
        loaded = ui.load_config(self.dcc).slots['1']
        self.assertEqual((loaded.local_name, loaded.context_window),
                         ('second', 800_000))

    def test_saving_blank_display_name_restores_picker_label(self):
        self.dcc.write_text(self.dcc.read_text().replace('1 = Beta\n', '1 = \n'))
        with patch.object(ui.P, 'restart', return_value=False), \
             patch.object(ui.CC, 'stop_all', return_value=0):
            result = self.editor.save(self.editor.state())
        self.assertTrue(result['changed'])
        self.assertFalse(ui._parser(self.dcc.read_bytes()).has_option('model_names', '1'))
        picker = json.loads(self.settings.read_text())['modelPicker']['options']
        self.assertEqual(picker[1]['label'], '#1 · second')

    def test_saving_legacy_suffix_removes_it_and_updates_picker(self):
        self.dcc.write_text(self.dcc.read_text().replace('[context]\n',
                            '[model_suffix]\n2 = [1m]\n[context]\n'))
        with patch.object(ui.P, 'restart', return_value=False), \
             patch.object(ui.CC, 'stop_all', return_value=0):
            result = self.editor.save(self.editor.state())
        self.assertTrue(result['changed'])
        self.assertNotIn('[model_suffix]', self.dcc.read_text())
        self.assertEqual(json.loads(self.settings.read_text())['model'], 'dcc-slot-2')

    def test_save_rolls_back_files_if_restart_fails(self):
        state = self.editor.state()
        state['models'][0]['fields']['model'] = 'changed'
        before = (self.dcc.read_bytes(), self.llm.read_bytes())
        with patch.object(ui.P, 'restart', side_effect=RuntimeError('restart failed')):
            with self.assertRaisesRegex(ui.SaveError, '文件已恢复'):
                self.editor.save(state)
        self.assertEqual((self.dcc.read_bytes(), self.llm.read_bytes()), before)
        self.assertEqual(stat.S_IMODE(self.llm.stat().st_mode), 0o644)

    def test_settings_sync_is_rolled_back_after_failed_restart(self):
        self.settings.parent.mkdir()
        original = b'{"theme":"dark"}\n'
        self.settings.write_bytes(original)
        state = self.editor.state()
        state['slots'].reverse()
        with patch.object(ui.P, 'restart', side_effect=RuntimeError('restart failed')):
            with self.assertRaisesRegex(ui.SaveError, '文件已恢复'):
                self.editor.save(state)
        self.assertEqual(self.settings.read_bytes(), original)
        self.assertIn('2 = first', self.dcc.read_text())

    def test_external_settings_edit_blocks_save_and_write_failure_preserves_files(self):
        self.settings.parent.mkdir()
        self.settings.write_text('{"theme":"dark"}\n')
        state = self.editor.state()
        state['models'][0]['fields']['model'] = 'changed'
        self.settings.write_text('{"theme":"light"}\n')
        with self.assertRaisesRegex(ValueError, '外部修改'):
            self.editor.save(state)
        self.assertIn('upstream-one', self.llm.read_text())
        state = self.editor.state()
        state['models'][0]['fields']['model'] = 'changed'
        with patch.object(ui, '_replace_atomic', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(ui.SaveError, '代理未重载'):
                self.editor.save(state)
        self.assertIn('upstream-one', self.llm.read_text())
        self.assertEqual(self.settings.read_text(), '{"theme":"light"}\n')

    def test_failed_reload_restores_previous_running_proxy(self):
        state = self.editor.state()
        state['models'][0]['fields']['model'] = 'changed'
        with patch.object(ui.P, 'read_pid', return_value=123), \
             patch.object(ui.P, 'status', return_value={'healthy': True}), \
             patch.object(ui.P, 'restart', side_effect=[RuntimeError('new failed'), False]) as restart:
            with self.assertRaisesRegex(ui.SaveError, '代理已恢复'):
                self.editor.save(state)
        self.assertEqual(restart.call_count, 2)
        self.assertIn('upstream-one', self.llm.read_text())

    def test_save_updates_picker_without_extra_files_and_noop_does_not_restart(self):
        self.settings.parent.mkdir()
        self.settings.write_text('{"theme":"dark"}\n')
        state = self.editor.state()
        state['slots'].reverse()
        state['models'][0]['fields']['model'] = 'new-upstream'
        with patch.object(ui.P, 'restart', return_value=False) as restart, \
             patch.object(ui.CC, 'stop_all', return_value=0):
            result = self.editor.save(state)
            self.assertTrue(result['changed'])
            self.assertEqual(stat.S_IMODE(self.llm.stat().st_mode), 0o600)
            self.assertEqual(restart.call_count, 1)
            self.assertIn('new-upstream', self.llm.read_text())
            self.assertIn('custom = original', self.llm.read_text())
            self.assertEqual(json.loads(self.settings.read_text())['model'], 'dcc-slot-1')
            self.assertIn('dark', self.settings.read_text())
            self.assertEqual(set(result), {'changed', 'stopped'})
            self.assertFalse(list(self.root.rglob('*.dcc-backup-*')))
            self.assertEqual(list(self.settings.parent.iterdir()), [self.settings])
            self.assertEqual(self.editor.save(self.editor.state())['changed'], False)
            self.assertEqual(restart.call_count, 1)

    def test_proxy_snapshot_follows_declared_slot_order(self):
        cfg = ui.load_config(self.dcc)
        ui.P.render_config_yaml(cfg)
        snapshot = json.loads(cfg.proxy_config_path.read_text())
        self.assertEqual([model['slot'] for model in snapshot['models']], ['2', '1'])

    def test_first_model_can_be_created_when_llm_ini_is_missing(self):
        self.llm.unlink()
        state = self.editor.state()
        state['models'] = [{'section': 'new', 'original_section': '',
                            'fields': {'base_url': 'http://gateway.invalid/v1',
                                       'model': 'upstream-new', 'protocol': 'openai'},
                            'api_key': 'new-secret'}]
        state['slots'] = [{'id': '1', 'section': 'new', 'name': '', 'context': ''}]
        state['groups'] = []
        with patch.object(ui.P, 'restart', return_value=False), \
             patch.object(ui.CC, 'stop_all', return_value=0):
            result = self.editor.save(state)
        self.assertTrue(result['changed'])
        self.assertIn('[new]', self.llm.read_text())

    def test_http_requires_token_and_same_origin(self):
        server, token = ui.create_server(self.editor, token='test-token')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with urllib.request.urlopen(base + '/icon.png') as response:
                self.assertEqual(response.headers['Content-Type'], 'image/png')
                self.assertEqual(response.read(), (Path(ui.__file__).parent.parent / 'docs/assets/dcc-icon-pink.png').read_bytes())
            with urllib.request.urlopen(base + '/getting-started.md') as response:
                self.assertEqual(response.headers['Content-Type'], 'text/plain; charset=utf-8')
                self.assertEqual(response.read(), (Path(ui.__file__).parent.parent / 'getting-started.md').read_bytes())
            with urllib.request.urlopen(base + '/guide') as response:
                self.assertEqual(response.headers['Content-Type'], 'text/html; charset=utf-8')
                guide = response.read().decode()
                self.assertIn('第一次使用，从这里开始', guide)
                self.assertIn('保存并重载', guide)
                self.assertNotIn('renderMarkdown', guide)
            with urllib.request.urlopen(base + '/guide?doc=start') as response:
                self.assertIn('renderMarkdown', response.read().decode())
            with urllib.request.urlopen(base + '/dcc.md') as response:
                self.assertIn('dcc', response.read().decode())
            request = urllib.request.Request(base + '/guide', headers={'Host': 'evil.invalid'})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 403)
            request = urllib.request.Request(base + '/getting-started.md', headers={'Host': 'evil.invalid'})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 403)
            request = urllib.request.Request(base + '/icon.png', headers={'Host': 'evil.invalid'})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 403)
            request = urllib.request.Request(base + '/docs/assets/dcc-icon-pink.png', headers={'X-DCC-Token': token})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 404)
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base + '/api/state')
            self.assertEqual(error.exception.code, 403)
            request = urllib.request.Request(base + '/api/state', headers={
                'X-DCC-Token': token, 'Origin': 'http://evil.invalid'})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 403)
            request = urllib.request.Request(base + '/api/state', headers={'X-DCC-Token': token})
            with urllib.request.urlopen(request) as response:
                self.assertNotIn('secret-one', response.read().decode())
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
            request = urllib.request.Request(base + '/api/usage?days=7', headers={'X-DCC-Token': token})
            with urllib.request.urlopen(request) as response:
                self.assertEqual(json.load(response)['total']['attempts'], 0)
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base + '/api/usage?days=7')
            self.assertEqual(error.exception.code, 403)
            request = urllib.request.Request(base + '/api/usage?days=8', headers={'X-DCC-Token': token})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 400)
            request = urllib.request.Request(base + '/api/key?section=first', headers={'X-DCC-Token': token})
            with urllib.request.urlopen(request) as response:
                self.assertEqual(json.load(response)['api_key'], 'secret-one')
            with urllib.request.urlopen(base + '/') as response:
                page = response.read().decode()
                self.assertIn('保存并重载', page)
                self.assertIn('href="/guide" target="_blank"', page)
                self.assertIn('用量看板', (Path(ui.__file__).with_name('ui.html')).read_text())
        finally:
            server.shutdown()
            server.server_close()

    def test_usage_dashboard_still_loads_without_model_config(self):
        self.llm.unlink()
        with patch.object(ui.P, 'restart') as restart, \
             patch.object(ui.CC, 'stop_all') as stop:
            self.assertEqual(self.editor.usage_summary(7)['total']['attempts'], 0)
        restart.assert_not_called()
        stop.assert_not_called()

    def test_draft_connection_uses_each_protocol_and_does_not_save(self):
        captured = []

        class Upstream(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                captured.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                if self.path == '/chat/completions':
                    events = [{'choices': [{'delta': {'content': 'OK'}, 'finish_reason': None}]},
                              {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}]
                elif self.path == '/responses':
                    events = [{'type': 'response.output_text.delta', 'delta': 'OK'},
                              {'type': 'response.completed'}]
                else:
                    events = [{'type': 'content_block_delta', 'delta': {'text': 'OK'}},
                              {'type': 'message_stop'}]
                data = b''.join(b'data: ' + json.dumps(event).encode() + b'\n\n' for event in events)
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        original = self.llm.read_bytes()
        try:
            for protocol, path in (('openai', '/chat/completions'),
                                   ('responses', '/responses'), ('anthropic', '/v1/messages')):
                item = self.editor.state()['models'][0]
                item['fields'].update(protocol=protocol, base_url=f'http://127.0.0.1:{server.server_port}',
                                      model='draft-model')
                result = self.editor.test_connection(item)
                self.assertTrue(result['ok'], result)
                self.assertEqual(captured[-1][0], path)
                self.assertEqual(captured[-1][1]['model'], 'draft-model')
                self.assertTrue(captured[-1][1]['stream'])
            self.assertEqual(self.llm.read_bytes(), original)
        finally:
            server.shutdown()
            server.server_close()

    def test_reasoning_only_token_limit_reports_connection_without_text(self):
        class Response:
            status_code = 200
            encoding = 'utf-8'

            def __init__(self, events):
                self.events = events

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def iter_lines(self, decode_unicode=False):
                for event in self.events:
                    yield 'data: ' + json.dumps(event)

        cases = {
            'openai': [
                {'choices': [{'delta': {'reasoning_content': 'thinking'}, 'finish_reason': None}]},
                {'choices': [{'delta': {}, 'finish_reason': 'length'}]},
            ],
            'responses': [
                {'type': 'response.reasoning_summary_text.delta', 'delta': 'thinking'},
                {'type': 'response.completed'},
            ],
            'anthropic': [
                {'type': 'content_block_delta', 'delta': {'type': 'thinking_delta', 'thinking': 'thinking'}},
                {'type': 'message_stop'},
            ],
        }
        for protocol, events in cases.items():
            with self.subTest(protocol=protocol), patch.object(ui.requests, 'post', return_value=Response(events)):
                item = self.editor.state()['models'][0]
                item['fields']['protocol'] = protocol
                result = self.editor.test_connection(item)
                self.assertTrue(result['ok'], result)
                self.assertIn('没有可见文本', result['message'])

        with patch.object(ui.requests, 'post', return_value=Response([{'type': 'response.completed'}])):
            item['fields']['protocol'] = 'responses'
            self.assertFalse(self.editor.test_connection(item)['ok'])

