"""CLI 分派:list / status / start (slot) / stop / stop all / resume。"""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import log as L
from . import proxy as P
from . import cc as CC
from .config import DccConfig, ModelSlot, load as load_config
from .version import DCC_VERSION
from .telemetry import Telemetry


HELP = f"""dcc v{DCC_VERSION} — 本地 CC 多模型代理管理器

用法:
  dcc list                          列所有已配 slot(dcc.ini [models])
  dcc status                        代理/实例运行状态
  dcc <slot> [-r|--resume [ID]]       起 slot 对应模型的 CC；-r 无 ID 进入恢复选择 · -r <ID> 直接恢复指定会话
  dcc <slot> "prompt"               一次性指令(快捷键,等价于 claude -p "prompt")
  dcc stop                          停全部 CC + 杀代理 + 重启(改完 dcc.ini 后必做)
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dcc", add_help=False)
    p.add_argument("cmd", nargs="?", default="")
    p.add_argument("arg", nargs="?", default="")
    p.add_argument("-r", "--resume", nargs="?", const="", default=None)
    p.add_argument("-h", "--help", action="store_true")
    p.add_argument("-v", "--version", action="store_true")
    return p


def main(argv: list[str]) -> int:
    t0 = L.start()
    parser = build_parser()
    ns, extra = parser.parse_known_args(argv)

    if ns.version:
        print(f"dcc {DCC_VERSION}", file=sys.stderr)
        L.done(t0, ok=True, cmd="version")
        return 0

    if ns.help or not ns.cmd:
        print(HELP, file=sys.stderr)
        L.done(t0, ok=True, cmd="help")
        return 0

    cfg = load_config()

    if ns.cmd == "list":
        return _cmd_list(cfg, t0)
    if ns.cmd == "status":
        return _cmd_status(cfg, t0)
    if ns.cmd == "stop":
        return _cmd_stop(cfg, t0)

    # dcc <slot> [--resume ID] [prompt]
    slot = cfg.slots.get(ns.cmd)
    if slot:
        # 参考 dge:dcc <slot> 后面直接跟非选项参数 → 视作一次性指令 prompt · 转 claude -p
        if ns.arg and not ns.arg.startswith("-"):
            extra = ["-p", ns.arg] + extra
        return _cmd_start_slot(cfg, slot, ns.resume, extra, t0)

    L.log(f"[ERROR] 未知命令: {ns.cmd}", level="ERROR")
    print(HELP, file=sys.stderr)
    L.done(t0, ok=False, rc=1, msg=f"unknown_cmd={ns.cmd}")
    return 1


def _cmd_list(cfg: DccConfig, t0: float) -> int:
    if not cfg.slots:
        L.log("dcc.ini [models] 未配置任何 slot")
    else:
        print(f"{'SLOT':<6}{'CLI_NAME':<34}{'LIMIT':<12}IMG", file=sys.stderr)
        for slot_id in sorted(cfg.slots.keys()):
            s = cfg.slots[slot_id]
            limit = s.daily_token_limit or "-"
            img = "✅" if s.supports_images else "❌"
            print(f"{s.slot:<6}{s.local_name:<34}{limit:<12}{img}", file=sys.stderr)
    L.done(t0, ok=True, slots=len(cfg.slots))
    return 0


def _cmd_status(cfg: DccConfig, t0: float) -> int:
    st = P.status(cfg)
    print("== dcc-proxy 代理 ==", file=sys.stderr)
    print(json.dumps(st, ensure_ascii=False, indent=2), file=sys.stderr)
    ins = CC.list_instances(cfg)
    print("\n== CC 实例(存活) ==", file=sys.stderr)
    if not ins:
        print("(无)", file=sys.stderr)
    else:
        print(json.dumps(ins, ensure_ascii=False, indent=2), file=sys.stderr)
    L.done(
        t0, ok=True,
        proxy_up=str(st["healthy"]).lower(),
        instances=len(ins),
    )
    return 0


def _cmd_stop(cfg: DccConfig, t0: float) -> int:
    """dcc stop — 停全部 CC + kill 代理 + 重渲染 config 后重启代理。"""
    n_cc = CC.stop_all(cfg)
    L.log(f"已停 CC 实例 × {n_cc}")
    proxy_killed = P.stop_proxy(cfg)
    L.log(f"代理已{'杀' if proxy_killed else '不在'}")
    # 等端口释放后重启
    deadline = time.time() + 10
    while P.is_port_open(cfg.port) and time.time() < deadline:
        time.sleep(0.3)
    P.start(cfg)
    L.done(t0, ok=True, cc_killed=n_cc, proxy_restarted=str(proxy_killed or True))
    return 0


def _cmd_start_slot(
    cfg: DccConfig, slot: ModelSlot, resume: str, extra: list[str], t0: float
) -> int:
    usage = Telemetry(cfg.output_dir / "telemetry", cfg.telemetry, DCC_VERSION)
    usage.track("dcc_launch", slot=slot.slot, protocol=slot.protocol,
                mode="resume" if resume is not None else ("prompt" if "-p" in extra else "interactive"))
    started = time.monotonic()
    try:
        P.start(cfg)
    except (Exception, SystemExit):
        usage.track("dcc_proxy_start", result="failed",
                    durationMs=round((time.monotonic() - started) * 1000))
        raise
    usage.track("dcc_proxy_start", result="ready",
                durationMs=round((time.monotonic() - started) * 1000))
    L.done(t0, ok=True, slot=slot.slot, model=slot.local_name, resume=resume or "-")
    # 之后 exec 替换进程,不再返回
    CC.spawn(cfg, slot, resume, extra)
    return 0  # 到不了
