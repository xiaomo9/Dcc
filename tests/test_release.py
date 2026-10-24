import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src import app_paths, config, platform_support


class ReleaseTests(unittest.TestCase):
    def test_source_config_path_unchanged(self):
        with patch.dict(os.environ, {}, clear=True), patch('sys.frozen', False, create=True):
            self.assertEqual(app_paths.config_path(), Path(__file__).resolve().parents[1] / 'dcc.ini')

    def test_first_run_empty_config_and_upgrade_preserve_user_data(self):
        with tempfile.TemporaryDirectory(prefix='dcc 中文 ') as temp:
            with patch.dict(os.environ, {'DCC_DATA_DIR': temp}):
                path = app_paths.config_path()
                cfg = config.load(path)
                self.assertEqual(cfg.slots, {})
                self.assertEqual(cfg.output_dir, Path(temp).resolve() / 'output')
                path.write_text(path.read_text(encoding='utf-8') + '\n[cc]\nbinary=custom\n', encoding='utf-8')
                original = path.read_bytes()
                (Path(temp) / 'llm.ini').write_text('[personal]\napi_key=secret\n', encoding='utf-8')
                self.assertEqual(app_paths.config_path().read_bytes(), original)
                self.assertIn('secret', (Path(temp) / 'llm.ini').read_text())

    def test_invalid_pid_never_probes_process_group(self):
        with patch('os.kill') as kill:
            self.assertFalse(platform_support.pid_alive(0))
            self.assertFalse(platform_support.pid_alive(-1))
            kill.assert_not_called()

    def test_desktop_reopens_existing_authenticated_panel(self):
        import json
        import threading
        from unittest.mock import MagicMock
        from src import desktop, ui
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'DCC_DATA_DIR': temp}):
            path = app_paths.config_path()
            server, token = ui.create_server(ui.ConfigEditor(path, Path(temp) / 'settings.json'))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            state = Path(temp) / 'panel.json'
            state.write_text(json.dumps({'pid': os.getpid(), 'port': server.server_port, 'token': token}))
            try:
                with patch.dict('sys.modules', {'tkinter': MagicMock()}), \
                        patch.object(desktop, 'create_server') as create, \
                        patch.object(desktop.webbrowser, 'open') as opened:
                    self.assertEqual(desktop.run(), 0)
                    create.assert_not_called()
                    opened.assert_called_once_with(f'http://127.0.0.1:{server.server_port}/#token={token}')
                    self.assertTrue(state.exists())
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_windows_launch_records_child_pid_and_returns_exit_code(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from src import cc
        cfg = SimpleNamespace(port=4000)
        slot = SimpleNamespace(local_name='test', slot='1', context_window=None)
        proc = MagicMock(pid=12345)
        proc.wait.return_value = 7
        proc.poll.return_value = 7
        with patch.object(cc.os, 'name', 'nt'), \
                patch.object(cc, 'resolve_cc_binary', return_value='claude.exe'), \
                patch.object(cc, 'clean_injected_history'), \
                patch.object(cc, '_record_instance') as record, \
                patch.object(cc, 'prune_dead') as prune, \
                patch.object(cc.subprocess, 'Popen', return_value=proc):
            self.assertEqual(cc.spawn(cfg, slot, None, []), 7)
            record.assert_called_once_with(cfg, slot, None, pid=12345)
            prune.assert_called_once_with(cfg)
            proc.terminate.assert_not_called()

    def test_process_probe_does_not_terminate_child(self):
        import subprocess
        import sys
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            self.assertTrue(platform_support.pid_alive(child.pid))
            self.assertIsNone(child.poll())
        finally:
            child.terminate()
            child.wait(timeout=5)
        self.assertFalse(platform_support.pid_alive(child.pid))

    def test_desktop_startup_failures_close_server_without_waiting(self):
        from unittest.mock import MagicMock
        from src import desktop
        for failure in ('write', 'chmod', 'thread'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp, \
                    patch.dict(os.environ, {'DCC_DATA_DIR': temp}):
                path = app_paths.config_path()
                server = MagicMock(server_port=12345)
                tk = MagicMock()
                target = {'write': (Path, 'write_text'), 'chmod': (Path, 'chmod'),
                          'thread': (desktop.threading.Thread, 'start')}[failure]
                with patch.dict('sys.modules', {'tkinter': tk}), \
                        patch.object(desktop, 'config_path', return_value=path), \
                        patch.object(desktop, 'create_server', return_value=(server, 'token')), \
                        patch.object(*target, side_effect=OSError('startup failed')):
                    self.assertEqual(desktop.run(), 1)
                server.shutdown.assert_not_called()
                server.server_close.assert_called_once()
                tk.Tk.return_value.destroy.assert_called_once()

    def test_desktop_closes_running_service_on_normal_exit(self):
        from unittest.mock import MagicMock
        from src import desktop
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'DCC_DATA_DIR': temp}):
            app_paths.config_path()
            server = MagicMock(server_port=12345)
            with patch.dict('sys.modules', {'tkinter': MagicMock()}), \
                    patch.object(desktop, 'create_server', return_value=(server, 'token')), \
                    patch.object(desktop.threading.Thread, 'start'), \
                    patch.object(desktop.webbrowser, 'open'):
                self.assertEqual(desktop.run(), 0)
            server.shutdown.assert_called_once()
            server.server_close.assert_called_once()
            self.assertFalse((Path(temp) / 'panel.json').exists())

    def test_frozen_windows_gui_uses_console_helper_for_proxy(self):
        from unittest.mock import MagicMock
        from src import proxy
        import sys
        for frozen, system, executable in (
                (True, 'win32', '/application/DCC.exe'),
                (True, 'darwin', '/application/DCC'),
                (False, sys.platform, sys.executable)):
            with self.subTest(frozen=frozen, system=system), tempfile.TemporaryDirectory() as temp, \
                    patch.dict(os.environ, {'DCC_DATA_DIR': temp}):
                cfg = config.load(app_paths.config_path())
                with patch.object(sys, 'frozen', frozen, create=True), \
                        patch.object(sys, 'platform', system), \
                        patch.object(sys, 'executable', executable), \
                        patch.object(proxy, 'is_port_open', return_value=False), \
                        patch.object(proxy, '_wait_ready', return_value=True), \
                        patch.object(proxy.subprocess, 'Popen', return_value=MagicMock(pid=12345)) as launch:
                    proxy.start(cfg, fixed_port=True)
                args = launch.call_args.args[0]
                if frozen:
                    expected = str(Path(executable).with_name('dcc-cli.exe')) if system == 'win32' else executable
                    self.assertEqual(args, [expected, '--serve-proxy', str(cfg.proxy_config_path)])
                else:
                    self.assertEqual(args, [executable, str(Path(proxy.__file__).parent / 'proxy_server.py'), str(cfg.proxy_config_path)])
                self.assertEqual(launch.call_args.kwargs['stdout'].name, str(cfg.proxy_log))
                self.assertIs(launch.call_args.kwargs['stdout'], launch.call_args.kwargs['stderr'])
