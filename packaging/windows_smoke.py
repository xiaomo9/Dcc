"""Run on a disposable Windows CI account after building the installer."""
import os
from pathlib import Path
import subprocess
import tempfile
import winreg


def read_path():
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
        try:
            return winreg.QueryValueEx(key, 'Path')
        except FileNotFoundError:
            return None


def write_path(value):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
        if value is None:
            winreg.DeleteValue(key, 'Path')
        else:
            winreg.SetValueEx(key, 'Path', 0, value[1], value[0])


installer = Path(__file__).resolve().parents[1] / 'dist/DCC-Windows-x64-Setup.exe'
original = read_path()
original_process_path = os.environ['PATH']
with tempfile.TemporaryDirectory(prefix='dcc installer 中文 ') as temporary:
    app = Path(temporary) / 'DCC'
    uninstaller = app / 'unins000.exe'

    def install(add_to_path=False):
        tasks = ['/TASKS=addtopath'] if add_to_path else []
        subprocess.run([str(installer), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                        '/CURRENTUSER', '/DIR=' + str(app), *tasks], check=True, timeout=90)

    def uninstall():
        subprocess.run([str(uninstaller), '/VERYSILENT', '/SUPPRESSMSGBOXES',
                        '/NORESTART'], check=True, timeout=90)

    try:
        for preexisting in (False, True):
            baseline = r'%USERPROFILE%\dcc-path-sentinel'
            if preexisting:
                baseline += ';"' + str(app).lower() + '\\"'
            write_path((baseline, winreg.REG_EXPAND_SZ))
            install()
            assert read_path() == (baseline, winreg.REG_EXPAND_SZ), 'Unchecked task changed Path'
            install(add_to_path=True)
            first = read_path()
            assert first[1] == winreg.REG_EXPAND_SZ, 'Path registry type changed'
            assert first[0].startswith(baseline), 'Existing Path was modified'
            (app / 'dcc-cli.exe').write_bytes(b'legacy helper placeholder')
            install(add_to_path=True)
            assert read_path() == first, 'Upgrade duplicated the Path entry'
            assert (app / 'DCC-panel.exe').is_file(), 'GUI executable missing'
            assert (app / 'dcc.exe').is_file(), 'CLI executable missing'
            assert not (app / 'dcc-cli.exe').exists(), 'Legacy helper was retained'
            install()
            assert read_path() == first, 'Unchecked upgrade modified existing Path'
            if preexisting:
                assert first[0] == baseline, 'Preexisting entry was duplicated'
            else:
                os.environ['PATH'] = original_process_path + ';' + first[0]
                result = subprocess.run(['dcc', '-v'], capture_output=True,
                                        text=True, encoding='utf-8', check=True, timeout=20)
                assert 'dcc ' in result.stdout + result.stderr, 'dcc command did not print its version'
            uninstall()
            assert read_path() == (baseline, winreg.REG_EXPAND_SZ), 'Uninstall modified existing Path entries'
        print('Windows installer optional Path, CLI, repeated upgrade and Path cleanup passed')
    finally:
        try:
            if uninstaller.exists():
                uninstall()
        finally:
            write_path(original)
            os.environ['PATH'] = original_process_path
