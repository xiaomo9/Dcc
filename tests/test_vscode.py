import json
import fcntl
import time
import http.server
import os
from pathlib import Path
import shutil
import signal
import site
import socket
import subprocess
import sys
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]


class VSCodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        shutil.copytree(ROOT / 'src', self.root / 'src',
                        ignore=shutil.ignore_patterns('__pycache__'))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        (self.root / 'llm.ini').write_text(
            '[one]\nbase_url=http://127.0.0.1:1\napi_key=test\nmodel=upstream-one\n'
            '[two]\nbase_url=http://127.0.0.1:1\napi_key=test\nmodel=upstream-two\n')
        (self.root / 'dcc.ini').write_text(
            f'[proxy]\nport={self.port}\nllm_ini_path={self.root}/llm.ini\n'
            f'[output]\ndir={self.root}/output\n'
            '[models]\n2=one\n1=two\n'
            '[model_names]\n2=first\n1=second\n')
        self.env = dict(os.environ, PYTHONPATH=str(self.root))

    def tearDown(self):
        pidfile = self.root / 'output/dcc-proxy.pid'
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass
        self.tmp.cleanup()

    def cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'src.dcc', *args],
                              cwd=self.root, env=self.env, capture_output=True,
                              text=True, timeout=15)

    def test_proxy_start_reuses_same_process_without_starting_claude(self):
        result = self.cli('start')
        self.assertEqual(result.returncode, 0, result.stderr)
        pid = (self.root / 'output/dcc-proxy.pid').read_text()
        result = self.cli('start')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'output/dcc-proxy.pid').read_text(), pid)
        self.assertFalse((self.root / 'output/instances.json').exists())

    def test_start_and_stop_wait_for_lifecycle_lock(self):
        output = self.root / 'output'
        output.mkdir()
        with (output / 'dcc-proxy.pid.lock').open('a') as lock:
            for command in ('start', 'stop'):
                fcntl.flock(lock, fcntl.LOCK_EX)
                proc = subprocess.Popen(
                    [sys.executable, '-m', 'src.dcc', command], cwd=self.root,
                    env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    time.sleep(1)
                    self.assertIsNone(proc.poll(), command + ' did not wait for lock')
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                    stdout, stderr = proc.communicate(timeout=15)
                self.assertEqual(proc.returncode, 0, stderr)

    def test_concurrent_starts_share_one_proxy(self):
        processes = [subprocess.Popen(
            [sys.executable, '-m', 'src.dcc', 'start'], cwd=self.root,
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(4)]
        outputs = [proc.communicate(timeout=15)[1] for proc in processes]
        for proc, stderr in zip(processes, outputs):
            self.assertEqual(proc.returncode, 0, stderr)
        self.assertEqual(sum('启动 dcc-proxy:' in out for out in outputs), 1)
        self.assertEqual(self.cli('stop').returncode, 0)
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', self.port), timeout=1)

    def test_reuse_warns_on_changed_config_without_restarting(self):
        self.assertEqual(self.cli('start').returncode, 0)
        pidfile = self.root / 'output/dcc-proxy.pid'
        pid = pidfile.read_text()
        snapshot = (self.root / 'output/config.json').read_bytes()
        result = self.cli('start')
        self.assertNotIn('配置已变化', result.stderr)
        llm = self.root / 'llm.ini'
        llm.write_text(llm.read_text().replace('api_key=test', 'api_key=changed-secret'))
        result = self.cli('start')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('配置已变化', result.stderr)
        self.assertIn('dcc stop', result.stderr)
        self.assertNotIn('changed-secret', result.stderr)
        self.assertEqual(pidfile.read_text(), pid)
        self.assertEqual((self.root / 'output/config.json').read_bytes(), snapshot)
        self.assertEqual(self.cli('stop').returncode, 0)
        self.assertEqual(self.cli('start').returncode, 0)
        self.assertNotIn('配置已变化', self.cli('start').stderr)

    def test_proxy_start_refuses_occupied_port(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', self.port))
            sock.listen()
            result = self.cli('start')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('端口', result.stderr)
        self.assertFalse((self.root / 'output/dcc-proxy.pid').exists())

    def test_stop_keeps_proxy_down_until_explicit_start(self):
        self.assertEqual(self.cli('start').returncode, 0)
        result = self.cli('stop')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / 'output/dcc-proxy.pid').exists())
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', self.port), timeout=1)
        result = self.cli('stop')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / 'output/dcc-proxy.pid').exists())
        self.assertEqual(self.cli('start').returncode, 0)

    def test_reset_reloads_config_and_stops_registered_instance(self):
        self.assertEqual(self.cli('start').returncode, 0)
        output = self.root / 'output'
        old_pid = (output / 'dcc-proxy.pid').read_text()
        instance = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        try:
            (output / 'instances.json').write_text(json.dumps({
                str(instance.pid): {'pid': instance.pid, 'slot': '1'}}))
            llm = self.root / 'llm.ini'
            llm.write_text(llm.read_text().replace('upstream-one', 'upstream-updated'))
            result = self.cli('reset')
            self.assertEqual(result.returncode, 0, result.stderr)
            instance.wait(timeout=5)
            self.assertNotEqual((output / 'dcc-proxy.pid').read_text(), old_pid)
            self.assertIn('upstream-updated', (output / 'config.json').read_text())
            self.assertEqual(json.loads((output / 'instances.json').read_text()), {})
            self.assertEqual(self.cli('start').returncode, 0)
            self.assertNotIn('配置已变化', self.cli('start').stderr)
        finally:
            if instance.poll() is None:
                instance.kill()
                instance.wait(timeout=5)

    def test_reset_starts_stopped_proxy_and_rejects_arguments(self):
        self.assertNotEqual(self.cli('reset', 'extra').returncode, 0)
        self.assertFalse((self.root / 'output/dcc-proxy.pid').exists())
        result = self.cli('reset')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'output/dcc-proxy.pid').exists())

    def test_reset_reports_port_still_occupied(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', self.port))
            sock.listen()
            result = self.cli('reset')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('端口仍被占用', result.stderr)
        self.assertFalse((self.root / 'output/dcc-proxy.pid').exists())

    def test_stop_terminates_registered_instance_without_starting_proxy(self):
        instance = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        try:
            output = self.root / 'output'
            output.mkdir()
            (output / 'instances.json').write_text(json.dumps({
                str(instance.pid): {'pid': instance.pid, 'slot': '1'}}))
            result = self.cli('stop')
            self.assertEqual(result.returncode, 0, result.stderr)
            instance.wait(timeout=5)
            self.assertEqual(json.loads((output / 'instances.json').read_text()), {})
            self.assertFalse((output / 'dcc-proxy.pid').exists())
        finally:
            if instance.poll() is None:
                instance.kill()
                instance.wait(timeout=5)

    def test_other_healthy_service_is_not_reused(self):
        class OtherService(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')

        server = http.server.ThreadingHTTPServer(('127.0.0.1', self.port), OtherService)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertNotEqual(self.cli('start').returncode, 0)
        finally:
            server.shutdown()
            server.server_close()

    def test_legacy_proxy_without_digest_warns_and_is_reused(self):
        class LegacyProxy(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"ok","service":"dcc-proxy","capabilities":["vscode"]}')

        server = http.server.ThreadingHTTPServer(('127.0.0.1', self.port), LegacyProxy)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = self.cli('start')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('无法确认配置是否一致', result.stderr)
            self.assertIn('dcc stop', result.stderr)
            self.assertFalse((self.root / 'output/dcc-proxy.pid').exists())
        finally:
            server.shutdown()
            server.server_close()

    def test_terminal_launch_still_moves_to_next_port(self):
        fake = self.root / 'claude'
        fake.write_text('#!' + sys.executable + '\n'
                        'import os, json, sys\n'
                        'print(json.dumps({"url":os.environ["ANTHROPIC_BASE_URL"],'
                        '"model":os.environ["ANTHROPIC_MODEL"],"args":sys.argv[1:]}))\n')
        fake.chmod(0o755)
        with (self.root / 'dcc.ini').open('a') as ini:
            ini.write(f'\n[cc]\nbinary={fake}\n')
        self.env['HOME'] = str(self.root)
        self.env['PYTHONPATH'] += os.pathsep + site.getusersitepackages()
        self.assertEqual(self.cli('settings').returncode, 0)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', self.port))
            sock.listen()
            result = self.cli('1')
        self.assertEqual(result.returncode, 0, result.stderr)
        captured = json.loads(result.stdout)
        self.assertEqual(captured['url'], f'http://127.0.0.1:{self.port + 1}')
        self.assertEqual(captured['model'], 'second[1m]')
        self.assertEqual(captured['args'][captured['args'].index('--model') + 1], 'second[1m]')
