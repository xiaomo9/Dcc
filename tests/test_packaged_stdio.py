"""Windows redirected streams must preserve Unicode startup diagnostics."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


class PackagedStdioTests(unittest.TestCase):
    def test_entry_outputs_utf8_under_legacy_codepage(self):
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run([sys.executable, str(ROOT / 'packaging/entry.py'), 'status'],
                                    cwd=ROOT, env=dict(os.environ, DCC_DATA_DIR=temp,
                                                     PYTHONPATH=str(ROOT), PYTHONIOENCODING='cp1252:strict'),
                                    capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode('ascii', 'backslashreplace'))
        self.assertIn('代理', result.stderr.decode('utf-8'))

    def test_proxy_starts_and_writes_chinese_logs_under_legacy_codepage(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'config.json'
            log = Path(temp) / 'proxy.log'
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            config.write_text(json.dumps({'port': port, 'models': []}), encoding='utf-8')
            with log.open('wb') as output:
                proc = subprocess.Popen([sys.executable, str(ROOT / 'src/proxy_server.py'), str(config)],
                                        env=dict(os.environ, PYTHONIOENCODING='cp1252:strict'),
                                        stdout=output, stderr=output)
            try:
                healthy = False
                deadline = time.monotonic() + 5
                while proc.poll() is None and time.monotonic() < deadline:
                    try:
                        with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=0.3) as response:
                            healthy = json.load(response)['service'] == 'dcc-proxy'
                            break
                    except OSError:
                        time.sleep(0.05)
                self.assertTrue(healthy, log.read_bytes().decode('ascii', 'backslashreplace'))
                self.assertIn('滚动保留', log.read_text(encoding='utf-8'))
            finally:
                if proc.poll() is None:
                    proc.terminate()
                proc.wait(timeout=5)

    def test_smoke_failure_shows_child_output_and_proxy_log(self):
        import contextlib
        import io
        import runpy
        from unittest.mock import patch

        def command(argv, **kwargs):
            root = Path(kwargs['env']['DCC_DATA_DIR'])
            if argv[-1] == 'status':
                (root / 'dcc.ini').write_text('[proxy]\nport=4000\n', encoding='utf-8')
            if argv[-1] == 'start':
                (root / 'output').mkdir()
                (root / 'output/dcc-proxy.log').write_text('UnicodeEncodeError: startup log failed', encoding='utf-8')
                raise subprocess.CalledProcessError(4, argv, output='child stdout', stderr='启动失败')
            return subprocess.CompletedProcess(argv, 0)

        output = io.StringIO()
        with patch.object(subprocess, 'run', side_effect=command), \
                contextlib.redirect_stderr(output), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(subprocess.CalledProcessError):
                runpy.run_path(str(ROOT / 'packaging/smoke.py'))
        self.assertIn('child stdout', output.getvalue())
        self.assertIn('启动失败', output.getvalue())
        self.assertIn('UnicodeEncodeError: startup log failed', output.getvalue())
