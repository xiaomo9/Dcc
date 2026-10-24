"""Exercise the frozen executable against disposable config and a free port."""
import configparser
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, 'reconfigure'):
        stream.reconfigure(encoding='utf-8', errors='backslashreplace')

root = Path(__file__).resolve().parents[1]
exe = root / 'dist/DCC' / ('dcc-cli.exe' if sys.platform == 'win32' else 'dcc-cli')
with tempfile.TemporaryDirectory(prefix='dcc-release-') as temporary:
    env = dict(os.environ, DCC_DATA_DIR=temporary)
    def run(*args, desktop=False):
        program = exe.with_name('DCC.exe' if sys.platform == 'win32' else 'DCC') if desktop else exe
        try:
            return subprocess.run([str(program), *args], env=env, capture_output=True,
                                  text=True, encoding='utf-8', errors='replace', timeout=45, check=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            print(f'Smoke command failed: {program.name} {args}', file=sys.stderr)
            for output in (error.stdout, error.stderr):
                if output:
                    print(output.decode('utf-8', 'replace') if isinstance(output, bytes) else output,
                          file=sys.stderr)
            log = Path(temporary) / 'output/dcc-proxy.log'
            if log.exists():
                print('--- proxy startup log ---', file=sys.stderr)
                print(log.read_text(encoding='utf-8', errors='replace')[-16000:], file=sys.stderr)
            raise
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
    log = Path(temporary) / 'output/dcc-proxy.log'
    log.write_bytes(b'')
    try:
        run('start', desktop=True)
        assert 'dcc-proxy listening on' in log.read_text(encoding='utf-8'), 'Desktop proxy startup log missing'
    finally:
        run('stop')
    print('Frozen CLI lifecycle and desktop proxy logging passed')
