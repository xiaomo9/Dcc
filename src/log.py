"""dcc 统一日志 + R17 结束语(参考 ogt/log.py)。"""
from __future__ import annotations
import sys
import time
from datetime import datetime

from .version import DCC_VERSION

_started_at: float | None = None


def start() -> float:
    global _started_at
    _started_at = time.time()
    # 每次 CLI 入口首行 · 打出 dcc 版本号(a.b.c) · 便于排查代码生效
    print(f"[{datetime.now().strftime('%H:%M:%S')} +0.0s] [dcc] version={DCC_VERSION}", file=sys.stderr, flush=True)
    return _started_at


def _elapsed() -> str:
    if _started_at is None:
        return "+0.0s"
    return f"+{time.time() - _started_at:.1f}s"


def _prefix() -> str:
    ts = datetime.now().strftime("%H:%M:%S")
    return f"[{ts} {_elapsed()}] [dcc]"


def log(msg: str, *, level: str = "INFO") -> None:
    prefix = _prefix() + " "
    if level != "INFO":
        prefix += f"[{level}] "
    print(prefix + msg, file=sys.stderr, flush=True)


def done(t0: float, *, ok: bool, rc: int = 0, **kv: object) -> None:
    """R17 结束语:`[HH:MM:SS +Ns] [dcc] DONE · 用时 Ns · 成功|失败 · k=v ...`"""
    dur = f"{time.time() - t0:.1f}s"
    status = "成功" if ok else "失败"
    parts = [f"{_prefix()} DONE", f"用时 {dur}", status]
    if not ok:
        parts.append(f"rc={rc}")
    for k, v in kv.items():
        parts.append(f"{k}={v}")
    print(" · ".join(parts), file=sys.stderr, flush=True)
