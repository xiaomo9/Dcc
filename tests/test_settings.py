import json
import os
from pathlib import Path
import shutil
import site
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src import settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / '.claude/settings.json'
        self.cfg = SimpleNamespace(port=4000, slots={
            '2': SimpleNamespace(slot='2', local_name='first', model_suffix='[1m]',
                                 base_url='https://one.invalid', api_key='secret-one'),
            '1': SimpleNamespace(slot='1', local_name='second', model_suffix='',
                                 base_url='https://two.invalid', api_key='secret-two'),
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_creates_all_slots_without_upstream_credentials(self):
        changed, backup = settings.write(self.cfg, self.path)
        self.assertTrue(changed)
        self.assertIsNone(backup)
        data = json.loads(self.path.read_text())
        self.assertEqual(data['model'], 'dcc-slot-2[1m]')
        self.assertEqual(data['env']['ANTHROPIC_MODEL'], 'dcc-slot-2[1m]')
        self.assertEqual(data['env']['ANTHROPIC_BASE_URL'], 'http://127.0.0.1:4000')
        self.assertEqual(data['env']['ANTHROPIC_AUTH_TOKEN'], 'dummy')
        self.assertEqual(data['env']['ANTHROPIC_CUSTOM_HEADERS'], 'x-dcc-client: vscode')
        self.assertEqual(data['modelPicker'], {
            'replaceBuiltInOptions': True,
            'options': [{'model': 'dcc-slot-2[1m]', 'label': '#2 · first'},
                        {'model': 'dcc-slot-1', 'label': '#1 · second'}]})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        for value in ('secret-one', 'secret-two', 'https://one.invalid', 'https://two.invalid'):
            self.assertNotIn(value, self.path.read_text())

    def test_merges_existing_settings_with_exact_backup_and_idempotence(self):
        original = {'env': {'KEEP': 'yes', 'ANTHROPIC_AUTH_TOKEN': 'old',
                            'CLAUDE_CODE_USE_BEDROCK': '1',
                            'ANTHROPIC_CUSTOM_HEADERS': 'X-Team: a\nx-DCC-client: old\nX-Trace: b'},
                    'theme': 'dark', 'permissions': {'deny': ['Read(secret)']},
                    'modelSettings': {'other': {'effort': 'high'}},
                    'model': 'old', 'modelPicker': {'options': []}}
        self.path.parent.mkdir()
        raw = json.dumps(original, indent=4).encode() + b'\n\n'
        self.path.write_bytes(raw)
        changed, backup = settings.write(self.cfg, self.path)
        self.assertTrue(changed)
        self.assertEqual(backup.read_bytes(), raw)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        data = json.loads(self.path.read_text())
        for key in ('theme', 'permissions', 'modelSettings'):
            self.assertEqual(data[key], original[key])
        self.assertEqual(data['env']['KEEP'], 'yes')
        self.assertEqual(data['env']['CLAUDE_CODE_USE_BEDROCK'], '0')
        self.assertEqual(data['env']['ANTHROPIC_CUSTOM_HEADERS'],
                         'X-Team: a\nX-Trace: b\nx-dcc-client: vscode')
        files = list(self.path.parent.iterdir())
        stamp = self.path.stat().st_mtime_ns
        self.assertEqual(settings.write(self.cfg, self.path), (False, None))
        self.assertCountEqual(list(self.path.parent.iterdir()), files)
        self.assertEqual(self.path.stat().st_mtime_ns, stamp)
        self.cfg.port = 4001
        _, second_backup = settings.write(self.cfg, self.path)
        self.assertNotEqual(backup, second_backup)
        self.assertEqual(backup.read_bytes(), raw)

    def test_invalid_settings_and_empty_slots_leave_original_untouched(self):
        self.path.parent.mkdir()
        for raw in ('{broken', '[]', 'null', '{"env": []}', '{"env": null}'):
            with self.subTest(raw=raw):
                self.path.write_text(raw)
                with self.assertRaises(ValueError):
                    settings.write(self.cfg, self.path)
                self.assertEqual(self.path.read_text(), raw)
                self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        self.path.unlink()
        self.cfg.slots = {}
        with self.assertRaisesRegex(ValueError, '槽位'):
            settings.write(self.cfg, self.path)
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_backup_and_atomic_replace_failures_keep_original(self):
        self.path.parent.mkdir()
        original = b'{"theme":"dark"}\n'
        self.path.write_bytes(original)
        with patch.object(settings.tempfile, 'NamedTemporaryFile', side_effect=OSError('backup failed')):
            with self.assertRaises(OSError):
                settings.write(self.cfg, self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        with patch.object(settings.os, 'replace', side_effect=OSError('replace failed')):
            with self.assertRaises(OSError):
                settings.write(self.cfg, self.path)
        self.assertEqual(self.path.read_bytes(), original)
        backups = list(self.path.parent.glob('settings.json.dcc-backup-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertEqual(len(list(self.path.parent.iterdir())), 2)

    def test_settings_command_writes_and_reports_backup_without_starting_proxy(self):
        root = Path(self.tmp.name)
        shutil.copytree(Path(__file__).resolve().parents[1] / 'src', root / 'src',
                        ignore=shutil.ignore_patterns('__pycache__'))
        (root / 'llm.ini').write_text(
            '[one]\nbase_url=https://one.invalid\napi_key=secret-one\nmodel=upstream-one\n'
            '[two]\nbase_url=https://two.invalid\napi_key=secret-two\nmodel=upstream-two\n')
        (root / 'dcc.ini').write_text(
            f'[proxy]\nllm_ini_path={root}/llm.ini\n[output]\ndir={root}/output\n'
            '[models]\n2=one\n1=two\n')
        env = dict(os.environ, HOME=str(root),
                   PYTHONPATH=str(root) + os.pathsep + site.getusersitepackages())
        self.path.parent.mkdir()
        self.path.write_text('{"theme":"dark"}')

        def run(*args):
            return subprocess.run([sys.executable, '-m', 'src.dcc', *args],
                                  cwd=root, env=env, text=True, capture_output=True, timeout=15)

        result = run('settings')
        self.assertEqual(result.returncode, 0, result.stderr)
        backup = next(self.path.parent.glob('settings.json.dcc-backup-*'))
        self.assertIn(str(backup), result.stderr)
        self.assertFalse((root / 'output/dcc-proxy.pid').exists())
        self.assertFalse((root / 'output/instances.json').exists())
        result = run('settings')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('无需更新', result.stderr)
        original = self.path.read_bytes()
        result = run('settings', '2')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.path.read_bytes(), original)
        self.path.write_text('{invalid')
        result = run('settings')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.path.read_text(), '{invalid')
