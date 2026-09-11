"""dcc 版本号 · 从同级 VERSION 文件读取(a.b.c 语义化)。"""
from __future__ import annotations

from pathlib import Path


def _read() -> str:
    p = Path(__file__).parent / "VERSION"
    try:
        return p.read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


DCC_VERSION = _read()
