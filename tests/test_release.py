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
