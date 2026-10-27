import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from src import desktop, shell_setup


class ShellSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dcc shell ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.command = self.root / "DCC 中文 '$` app/Contents/MacOS/dcc"
        self.command.parent.mkdir(parents=True)
        self.command.write_text('#!/bin/sh\nprintf "dcc-test-ok"\n')
        self.command.chmod(0o700)
        self.profile = self.root / '.zshrc'

    def test_profile_selection_supports_zsh_custom_directory_and_bash(self):
        with patch.object(Path, 'home', return_value=self.root), patch.dict(os.environ, {}, clear=True):
            self.assertEqual(shell_setup.profile_path(), self.profile)
            with patch.dict(os.environ, {'SHELL': '/bin/zsh', 'ZDOTDIR': str(self.root / 'custom')}):
                self.assertEqual(shell_setup.profile_path(), self.root / 'custom/.zshrc')
            with patch.dict(os.environ, {'SHELL': '/bin/bash'}):
                self.assertEqual(shell_setup.profile_path(), self.root / '.bash_profile')
                (self.root / '.profile').touch()
                self.assertEqual(shell_setup.profile_path(), self.root / '.profile')
            with patch.dict(os.environ, {'SHELL': '/bin/fish'}), self.assertRaises(ValueError):
                shell_setup.profile_path()

    def test_configuration_preserves_contents_and_is_idempotent(self):
        original = '# 我的配置\nexport EXISTING=keep'
        self.profile.write_text(original)
        self.profile.chmod(0o640)
        shell_setup.configure_path(self.command, self.profile)
        first = self.profile.read_bytes()
        self.assertTrue(first.decode().startswith(original + '\n'))
        self.assertEqual(self.profile.stat().st_mode & 0o777, 0o640)
        with patch.object(shell_setup, '_replace_atomic') as write:
            shell_setup.configure_path(self.command, self.profile)
            write.assert_not_called()

    def test_new_shell_finds_command_with_spaces_quotes_and_no_duplicate_path(self):
        shell_setup.configure_path(self.command, self.profile)
        for shell in ('zsh', 'bash'):
            executable = shutil.which(shell)
            if not executable:
                continue
            with self.subTest(shell=shell):
                result = subprocess.run([executable, '-c',
                    '. "$1"; . "$1"; dcc; printf "\\n%s" "$PATH"', 'test', str(self.profile)],
                    env=dict(os.environ, PATH='/usr/bin:/bin'), capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                output, path = result.stdout.split('\n', 1)
                self.assertEqual(output, 'dcc-test-ok')
                self.assertEqual(path.split(':').count(str(self.command.parent.resolve())), 1)

    def test_moved_installation_replaces_only_managed_block(self):
        shell_setup.configure_path(self.command, self.profile)
        self.profile.write_text(self.profile.read_text() + '# retain this\n')
        moved = self.root / 'new location/dcc'
        moved.parent.mkdir()
        shutil.copy2(self.command, moved)
        shell_setup.configure_path(moved, self.profile)
        text = self.profile.read_text()
        self.assertEqual(text.count(shell_setup.BEGIN), 1)
        self.assertNotIn(str(self.command.parent), text)
        self.assertIn(str(moved.parent.resolve()), text)
        self.assertTrue(text.endswith('# retain this\n'))

    def test_invalid_block_or_missing_executable_does_not_write(self):
        for text in ('# >>> DCC PATH >>>\n', '# <<< DCC PATH <<<\n'):
            self.profile.write_text(text)
            with self.assertRaises(ValueError):
                shell_setup.configure_path(self.command, self.profile)
            self.assertEqual(self.profile.read_text(), text)
        self.command.unlink()
        with self.assertRaises(ValueError):
            shell_setup.configure_path(self.command, self.profile)

    def test_cancel_does_not_write_and_write_failure_is_reported(self):
        messages = MagicMock()
        tkinter = MagicMock(messagebox=messages)
        with patch.dict('sys.modules', {'tkinter': tkinter}), \
                patch.object(shell_setup, 'profile_path', return_value=self.profile), \
                patch.object(shell_setup, 'configure_path') as configure:
            messages.askyesno.return_value = False
            desktop._configure_terminal()
            configure.assert_not_called()
            messages.askyesno.return_value = True
            configure.side_effect = PermissionError('read only')
            desktop._configure_terminal()
            messages.showerror.assert_called_once()
            messages.showinfo.assert_not_called()

    def test_atomic_write_failure_preserves_profile_and_cleans_temporary_file(self):
        self.profile.write_text('# existing\n')
        with patch('src.ui.os.replace', side_effect=PermissionError('read only')), self.assertRaises(OSError):
            shell_setup.configure_path(self.command, self.profile)
        self.assertEqual(self.profile.read_text(), '# existing\n')
        self.assertEqual(list(self.root.glob('.zshrc.*')), [])
