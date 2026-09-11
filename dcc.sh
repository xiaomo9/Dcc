#!/usr/bin/env bash
# dcc — 本地 CC 多模型代理管理器 入口
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec python3.11 "$ROOT/src/dcc.py" "$@"
