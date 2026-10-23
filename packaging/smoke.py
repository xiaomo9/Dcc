"""Exercise the frozen executable against disposable config and a free port."""
import configparser
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
exe = root / 'dist/DCC' / ('dcc-cli.exe' if sys.platform == 'win32' else 'dcc-cli')
with tempfile.TemporaryDirectory(prefix='dcc-release-') as temporary:
    env = dict(os.environ, DCC_DATA_DIR=temporary)
    def run(*args):
        return subprocess.run([str(exe), *args], env=env, capture_output=True,
                              text=True, timeout=45, check=True)
    run('status')
    ini = Path(temporary) / 'dcc.ini'
    parser = configparser.ConfigParser()
    parser.read(ini, encoding='utf-8')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        parser['proxy']['port'] = str(sock.getsockname()[1])
    with ini.open('w', encoding='utf-8') as out:
        parser.write(out)
    try:
        run('start')
        first = (Path(temporary) / 'output/dcc-proxy.pid').read_text()
        run('start')
        assert (Path(temporary) / 'output/dcc-proxy.pid').read_text() == first
        run('reset')
    finally:
        run('stop')
    print('Frozen CLI start/reuse/reset/stop passed')
