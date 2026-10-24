import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from src import proxy


POPEN = subprocess.Popen


class ProxyStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.cfg = SimpleNamespace(port=4000, proxy_pidfile=root / 'proxy.pid',
                                   proxy_log=root / 'proxy.log', proxy_config_path=root / 'config.json')
        self.children = []
        for name, value in (('is_port_open', False), ('render_config_yaml', None)):
            mock = patch.object(proxy, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)

    def launch(self, *args, **kwargs):
        child = POPEN([sys.executable, '-c', 'import time; time.sleep(60)'], **kwargs)
        self.children.append(child)
        return child

    def test_timeout_reaps_real_child_and_allows_retry(self):
        with patch.object(proxy.subprocess, 'Popen', side_effect=self.launch), \
                patch.object(proxy, '_wait_ready', return_value=False):
            with self.assertRaises(SystemExit) as error:
                proxy.start(self.cfg, fixed_port=True)
        self.assertEqual(error.exception.code, 4)
        self.assertIsNotNone(self.children[0].poll())
        self.assertFalse(self.cfg.proxy_pidfile.exists())
        with patch.object(proxy.subprocess, 'Popen', side_effect=self.launch), \
                patch.object(proxy, '_wait_ready', return_value=True):
            pid = proxy.start(self.cfg, fixed_port=True)
        self.assertEqual(pid, self.children[1].pid)
        self.assertIsNone(self.children[1].poll())
        self.assertEqual(self.cfg.proxy_pidfile.read_text(), str(pid))

    def test_pid_write_failure_reaps_child_and_preserves_error(self):
        with patch.object(proxy.subprocess, 'Popen', side_effect=self.launch), \
                patch.object(Path, 'write_text', side_effect=PermissionError('pid write denied')):
            with self.assertRaisesRegex(PermissionError, 'pid write denied'):
                proxy.start(self.cfg)
        self.assertIsNotNone(self.children[0].poll())

    def test_interrupt_during_readiness_also_cleans_up(self):
        with patch.object(proxy.subprocess, 'Popen', side_effect=self.launch), \
                patch.object(proxy, '_wait_ready', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                proxy.start(self.cfg)
        self.assertIsNotNone(self.children[0].poll())
        self.assertFalse(self.cfg.proxy_pidfile.exists())

    def test_cleanup_does_not_touch_another_pid_file(self):
        def readiness(cfg):
            cfg.proxy_pidfile.write_text('987654')
            return False
        with patch.object(proxy.subprocess, 'Popen', side_effect=self.launch), \
                patch.object(proxy, '_wait_ready', side_effect=readiness):
            with self.assertRaises(SystemExit):
                proxy.start(self.cfg)
        self.assertIsNotNone(self.children[0].poll())
        self.assertEqual(self.cfg.proxy_pidfile.read_text(), '987654')

    def test_force_kill_and_cleanup_failure_keep_original_error(self):
        for cleanup_failed in (False, True):
            with self.subTest(cleanup_failed=cleanup_failed):
                child = MagicMock(pid=12345)
                child.poll.return_value = None
                timeout = subprocess.TimeoutExpired('proxy', 2)
                child.wait.side_effect = [timeout, timeout] if cleanup_failed else [timeout, 0]
                with patch.object(proxy.subprocess, 'Popen', return_value=child), \
                        patch.object(proxy, '_wait_ready', side_effect=RuntimeError('original failure')):
                    with self.assertRaisesRegex(RuntimeError, 'original failure'):
                        proxy.start(self.cfg)
                child.terminate.assert_called_once()
                child.kill.assert_called_once()
                self.assertEqual(child.wait.call_count, 2)
                self.assertEqual(self.cfg.proxy_pidfile.exists(), cleanup_failed)

    def test_reused_proxy_is_not_terminated(self):
        self.cfg.proxy_pidfile.write_text(str(os.getpid()))
        with patch.object(proxy, 'is_port_open', return_value=True), \
                patch.object(proxy, '_is_dcc_proxy', return_value=True), \
                patch.object(proxy, '_warn_config_change'), \
                patch.object(proxy.subprocess, 'Popen') as launch:
            self.assertEqual(proxy.start(self.cfg, fixed_port=True), os.getpid())
        launch.assert_not_called()
        self.assertEqual(self.cfg.proxy_pidfile.read_text(), str(os.getpid()))
