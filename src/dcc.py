#!/usr/bin/env python3
"""dcc — 本地 CC 多模型代理管理器(瘦入口,业务逻辑在 src/)。

用法见 `bash tools/dcc/dcc.sh --help` 或 `dcc.md`。
"""
import sys

from src.cli import main


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
