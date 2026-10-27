"""Configure the installed macOS command after explicit user confirmation."""
import os
from pathlib import Path
import re
import shlex
import stat

from .ui import _replace_atomic

BEGIN = '# >>> DCC PATH >>>'
END = '# <<< DCC PATH <<<'


def profile_path() -> Path:
    shell = Path(os.environ.get('SHELL') or '/bin/zsh').name
    home = Path.home()
    if shell == 'zsh':
        return Path(os.environ.get('ZDOTDIR') or home).expanduser() / '.zshrc'
    if shell == 'bash':
        return next((home / name for name in ('.bash_profile', '.bash_login', '.profile')
                     if (home / name).exists()), home / '.bash_profile')
    raise ValueError('自动配置支持 zsh 和 bash；请手动将 App 内的 Contents/MacOS 目录加入 Path。')


def configure_path(executable: Path, profile: Path) -> None:
    executable = executable.resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError('未找到 dcc 命令，请确认安装完整。')
    directory = str(executable.parent)
    if ':' in directory or '\n' in directory or '\r' in directory:
        raise ValueError('安装目录含有 Path 不支持的字符，请更换安装位置。')
    profile = profile.expanduser().resolve()
    raw = profile.read_bytes() if profile.exists() else b''
    text = raw.decode('utf-8')
    quoted = shlex.quote(directory)
    block = f'{BEGIN}\ncase ":$PATH:" in\n  *:{quoted}:*) ;;\n  *) export PATH={quoted}:"$PATH" ;;\nesac\n{END}\n'
    pattern = re.compile(r'^' + re.escape(BEGIN) + r'\n.*?^' + re.escape(END) + r'(?:\n|$)', re.M | re.S)
    if BEGIN in text or END in text:
        if text.count(BEGIN) != 1 or text.count(END) != 1 or not pattern.search(text):
            raise ValueError('现有 DCC Path 配置不完整，请手动检查后重试。')
        updated = pattern.sub(lambda _: block, text, count=1)
    else:
        updated = text + ('\n' if text and not text.endswith('\n') else '') + block
    if updated.encode('utf-8') != raw:
        mode = stat.S_IMODE(profile.stat().st_mode) if profile.exists() else 0o600
        _replace_atomic(profile, updated.encode('utf-8'), mode)
