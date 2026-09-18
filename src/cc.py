"""CC 子进程编排:instances.json 落盘 + 前台起 claude。

设计要点(与 dcc.md 对齐):
- 每个 slot(1/2/3/...) → 一个 CC 实例,session 目录:<session_root>/<slot>-<local_name>
- 前台运行(用户在终端交互 · 不 daemon 化):dcc <slot> 直接 exec claude,不 fork
- 停止:只能停 daemon 化实例 · 前台由用户 Ctrl-C(仍记录到 instances.json 便于 status 展示)
- 会话恢复 `--resume <session_id>`:透传 claude CLI 的 resume 参数
- 环境变量清单:见 dcc.md 「多cc并行实例规则」段
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import time
from pathlib import Path

from .config import DccConfig, ModelSlot
from . import log as L


def resolve_cc_binary(cfg: DccConfig) -> str:
    """定位 claude 可执行文件。cfg.cc_binary > shutil.which('claude') > 报错。"""
    if cfg.cc_binary and Path(cfg.cc_binary).exists():
        return cfg.cc_binary
    p = shutil.which("claude")
    if p:
        return p
    L.log("[ERROR] 找不到 claude CLI,请设 dcc.ini [cc] binary", level="ERROR")
    raise SystemExit(5)


def _load_instances(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        L.log(f"[WARN] instances.json 损坏,忽略", level="WARN")
        return {}


def _save_instances(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def prune_dead(cfg: DccConfig) -> dict:
    data = _load_instances(cfg.instances_file)
    alive = {k: v for k, v in data.items() if v.get("pid") and _pid_alive(int(v["pid"]))}
    if len(alive) != len(data):
        _save_instances(cfg.instances_file, alive)
    return alive


# 代理注入的切换提示模板(见 proxy_server.py failover 主循环):
#   [dcc] 主模型 <X> 首字节 <N>s 未响应 · 已切至 <Y>\n\n<真实回复>
# 该行被 cc 存进 assistant 正文历史,回传时诱导模型模仿 → 无切换也自行冒这句。
# 启动前从历史剥掉此前缀:模型无样本可抄,根治"瞎提示"。真实回复内容不动。
_SWITCH_NOTICE_RE = re.compile(
    r"^\[dcc\] 主模型 .+? 首字节 \d+s 未响应 · 已切至 \S+[ \t]*\n+"
)
_HISTORY_WRITE_GRACE = 10.0  # 秒 · 跳过近期改动过的 jsonl(可能有 cc 正在写)


def _strip_switch_notice(text: str) -> str:
    """剥掉 assistant 正文开头的 dcc 注入前缀(可能被模型连抄多次)。"""
    while True:
        new = _SWITCH_NOTICE_RE.sub("", text, count=1)
        if new == text:
            return text
        text = new


def _clean_history_line(raw: str) -> str | None:
    """清洗一行 jsonl · 有改动返回新行,无改动返回 None(避免无谓重写)。"""
    try:
        rec = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    msg = rec.get("message")
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return None
    content = msg.get("content")
    if not isinstance(content, list):
        return None
    changed = False
    for block in content:
        if not (isinstance(block, dict) and block.get("type") == "text"):
            continue
        original = block.get("text")
        if not isinstance(original, str) or "已切至" not in original:
            continue
        stripped = _strip_switch_notice(original)
        if stripped != original:
            block["text"] = stripped
            changed = True
    if not changed:
        return None
    return json.dumps(rec, ensure_ascii=False)


def _encode_project_dir(cwd: Path) -> str:
    """cc 的项目历史目录编码:路径里非字母数字全替换成 -(实测规则)。"""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def clean_injected_history(cwd: Path | None = None) -> None:
    """启动 cc 前 · 剥掉本项目历史 jsonl 里的 dcc 切换提示前缀。

    只改 role=assistant 的 text block 开头的标准注入模板,真实回复不动;
    近 _HISTORY_WRITE_GRACE 秒改动过的文件跳过(避让正在写的 cc)· 原子重写。
    失败静默:清洗是尽力而为的优化,绝不能阻断 cc 启动。
    """
    try:
        base = Path("~/.claude/projects").expanduser() / _encode_project_dir(cwd or Path.cwd())
        if not base.is_dir():
            return
        now = time.time()
        for jf in base.glob("*.jsonl"):
            try:
                if now - jf.stat().st_mtime < _HISTORY_WRITE_GRACE:
                    continue
                lines = jf.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            out: list[str] = []
            touched = False
            for ln in lines:
                if not ln.strip():
                    out.append(ln)
                    continue
                cleaned = _clean_history_line(ln)
                if cleaned is None:
                    out.append(ln)
                else:
                    out.append(cleaned)
                    touched = True
            if not touched:
                continue
            tmp = jf.with_suffix(jf.suffix + ".tmp")
            try:
                tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
                os.replace(tmp, jf)
                L.log(f"清洗历史注入前缀 · {jf.name}")
            except OSError:
                tmp.unlink(missing_ok=True)
    except OSError:
        pass


def spawn(cfg: DccConfig, slot: ModelSlot, resume: str | None, extra_args: list[str]) -> int:
    """前台起 claude · 用 os.execvpe 替换当前进程 · 返回不到(交给 shell)。

    不改 CLAUDE_CONFIG_DIR:让 claude 走默认 ~/.claude/(共用 settings/agents/主题/项目历史);
    会话与 project 隔离交给 claude 自身按 cwd 分:每个 cwd 落 ~/.claude/projects/<encoded>/。
    """
    cc = resolve_cc_binary(cfg)

    env = os.environ.copy()
    for k in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_SUBSCRIPTION_TYPE",
    ):
        env.pop(k, None)
    cc_model = f"{slot.local_name}{slot.model_suffix}"
    env["ANTHROPIC_AUTH_TOKEN"] = "dummy"
    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{cfg.port}"
    env["ANTHROPIC_MODEL"] = cc_model

    # settings.json 里 env 段会被 claude 二次加载覆盖 dcc 注入 · 用 --settings 内联
    override_env = {
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{cfg.port}",
        "ANTHROPIC_AUTH_TOKEN": "dummy",
        "ANTHROPIC_MODEL": cc_model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": cc_model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME": cc_model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": cc_model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME": cc_model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": cc_model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME": cc_model,
    }
    settings = {"env": override_env}
    if slot.context_window:
        settings.update({"autoCompactEnabled": True, "autoCompactWindow": slot.context_window})
        L.log(f"dcc #{slot.slot} · Claude Code 自动压缩窗口={slot.context_window}")
    settings_arg = json.dumps(settings, ensure_ascii=False)

    argv = [cc, "--settings", settings_arg, "--model", cc_model]
    if resume is not None:
        argv.append("--resume")
        if resume:
            argv.append(resume)
    argv += extra_args

    _record_instance(cfg, slot, resume)
    clean_injected_history()  # 启动前剥掉历史里的 dcc 切换提示前缀 · 防模型模仿瞎提示
    L.log(
        f"启动 CC #{slot.slot} · model={slot.local_name} · cwd={os.getcwd()}"
        + (f" · resume={resume}" if resume else "")
    )
    os.execvpe(cc, argv, env)  # noqa: SLF001 (never returns)


def _record_instance(cfg: DccConfig, slot: ModelSlot, resume: str | None) -> None:
    data = prune_dead(cfg)
    data[slot.slot] = {
        "slot": slot.slot,
        "model": slot.local_name,
        "protocol": slot.protocol,
        "upstream": slot.upstream_model_id,
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "started_at": _now_str(),
        "mode": "foreground",
        "resume": resume or "",
    }
    _save_instances(cfg.instances_file, data)


def stop(cfg: DccConfig, slot_id: str) -> bool:
    """停某 slot(仅当前 dcc 记录里 pid 存活时才 kill)。"""
    data = prune_dead(cfg)
    ent = data.get(slot_id)
    if not ent:
        L.log(f"CC #{slot_id} 未在跑")
        return False
    pid = int(ent["pid"])
    if pid == os.getpid():
        L.log(f"CC #{slot_id} 是当前进程 · 拒绝自杀")
        return False
    L.log(f"停止 CC #{slot_id} pid={pid}")
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        L.log(f"[WARN] SIGTERM 失败: {e}", level="WARN")
    data.pop(slot_id, None)
    _save_instances(cfg.instances_file, data)
    return True


def stop_all(cfg: DccConfig) -> int:
    data = prune_dead(cfg)
    n = 0
    for slot_id in list(data.keys()):
        if stop(cfg, slot_id):
            n += 1
    return n


def list_instances(cfg: DccConfig) -> dict:
    return prune_dead(cfg)


def _now_str() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
