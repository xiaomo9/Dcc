"""Exercise the smoke script's uninstall boundary without a Windows registry."""
import ast
import contextlib
import io
from pathlib import Path
import unittest
from unittest.mock import MagicMock


class WindowsUninstallTests(unittest.TestCase):
    def environment(self):
        source = Path(__file__).resolve().parents[1] / 'packaging/windows_smoke.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        block = next(node for node in ast.walk(tree)
                     if isinstance(node, ast.FunctionDef) and node.name == 'uninstall')
        env = {'uninstaller': MagicMock(), 'uninstall_log': MagicMock(),
               'subprocess': MagicMock(), 'time': MagicMock(), 'read_path': MagicMock()}
        env['uninstall_log'].exists.return_value = False
        env['time'].monotonic.return_value = 0
        exec(compile(ast.Module(body=[block], type_ignores=[]), str(source), 'exec'), env)
        return env

    def test_waits_for_late_path_cleanup_even_after_exe_disappears(self):
        env = self.environment()
        expected = ('original', 2)
        env['uninstaller'].exists.return_value = False
        env['read_path'].side_effect = [('original;dcc', 2), expected]
        env['uninstall'](expected)
        env['time'].sleep.assert_called_once_with(0.1)
        env['subprocess'].run.assert_called_once()
        self.assertTrue(env['uninstall_started'])

    def test_waits_for_uninstaller_disappearance_even_if_path_is_restored(self):
        env = self.environment()
        env['uninstaller'].exists.side_effect = [True, False]
        env['read_path'].return_value = ('original', 2)
        env['uninstall'](('original', 2))
        env['time'].sleep.assert_called_once_with(0.1)

    def test_timeout_keeps_strict_path_type_check_and_prints_log(self):
        env = self.environment()
        env['uninstaller'].exists.return_value = False
        env['read_path'].return_value = ('original', 1)
        env['time'].monotonic.side_effect = [0, 31]
        env['uninstall_log'].exists.return_value = True
        env['uninstall_log'].read_text.return_value = 'uninstall diagnostic'
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(AssertionError):
            env['uninstall'](('original', 2))
        self.assertIn("actual Path=('original', 1)", output.getvalue())
        self.assertIn('uninstall diagnostic', output.getvalue())
        env['subprocess'].run.assert_called_once()
