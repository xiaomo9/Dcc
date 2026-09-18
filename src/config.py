"""dcc.ini 加载 + 归一化。所有模型三要素来自 ~/.dm/llm.ini(单点权威)。"""
from __future__ import annotations

import configparser
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ModelSlot:
    slot: str
    local_name: str          # cc 端 ANTHROPIC_MODEL 用这个
    model_suffix: str        # cc 端模型名后缀，如 [1m]
    section: str             # llm.ini 里的 section 名
    base_url: str            # llm.ini section.base_url · 决定走哪个 endpoint
    api_key: str             # llm.ini section.api_key
    upstream_model_id: str   # llm.ini section.model · 网关侧真实 id
    protocol: str            # openai / anthropic / responses
    context_window: int | None  # Claude Code context window, tokens
    supports_images: bool = False  # 是否支持图文输入 · 缺省 false(保守 · 见 ~/.dm/llm.ini section.supports_images)
    daily_token_limit: str = ""    # 展示用 · 每日 token 配额 · 常见值:2000M / 300M / unlimited / rag / 空
                                    # 权威 · ~/.dm/llm.ini section.daily_token_limit
    group: str = ""                # 所属等价组名(dcc.ini [groups]) · 空=不参与自动切换
    candidates: list[str] = field(default_factory=list)  # failover 候选(同组同协议) · local_name 列表 · 本槽之后顺序+回绕


@dataclass
class FailoverConfig:
    soft_timeout: float = 12.0   # 首字节软超时秒数 · 超过判定主模型慢 · 切下一候选(仅首帧前)
    switch_on_429: bool = True   # 首帧前遇 429/限流 · true=立即切候选 · false=先退避重试同模型
    max_candidates: int = 3      # 单请求最多尝试的候选数(含主模型) · 防雪崩


@dataclass
class DccConfig:
    port: int
    output_dir: Path
    proxy_config_path: Path
    proxy_pidfile: Path
    proxy_log: Path
    instances_file: Path
    session_root: Path
    cc_binary: str
    ready_timeout: int
    retention_days: int = 3
    slots: dict[str, ModelSlot] = field(default_factory=dict)
    failover: FailoverConfig = field(default_factory=FailoverConfig)


def load(ini_path: Path | None = None) -> DccConfig:
    ini = ini_path or (Path(__file__).parent.parent / "dcc.ini")
    if not ini.exists():
        print(f"❌ dcc.ini 不存在: {ini}", file=sys.stderr)
        sys.exit(2)

    p = configparser.ConfigParser()
    p.read(ini)

    port = p.getint("proxy", "port", fallback=4000)
    llm_ini_path = Path(p.get("proxy", "llm_ini_path", fallback="~/.dm/llm.ini")).expanduser()
    llm = _load_llm_ini(llm_ini_path)

    output_dir = Path(p.get("output", "dir", fallback="output/dcc")).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    proxy_config_path = output_dir / p.get("proxy", "proxy_config_name", fallback="config.json")
    proxy_pidfile = output_dir / p.get("proxy", "pidfile_name", fallback="dcc-proxy.pid")
    proxy_log = output_dir / p.get("proxy", "proxy_log_name", fallback="dcc-proxy.log")

    cc_binary = p.get("cc", "binary", fallback="").strip()
    session_root = Path(p.get("cc", "session_root", fallback=str(output_dir / "sessions"))).expanduser()
    session_root.mkdir(parents=True, exist_ok=True)
    instances_file = Path(
        p.get("cc", "instances_file", fallback=str(output_dir / "instances.json"))
    ).expanduser()

    default_model_suffix = p.get("defaults", "model_suffix", fallback="[1m]").strip()
    default_context_window = p.get("defaults", "context_window", fallback="800k")

    failover = FailoverConfig(
        soft_timeout=p.getfloat("failover", "soft_timeout", fallback=12.0),
        switch_on_429=_parse_bool(p.get("failover", "switch_on_429", fallback="true")),
        max_candidates=p.getint("failover", "max_candidates", fallback=3),
    )

    slots: dict[str, ModelSlot] = {}
    if p.has_section("models"):
        default_keys = set(p.defaults().keys())
        for slot in p.options("models"):
            if slot in default_keys:
                continue
            section = p.get("models", slot).strip()
            if section not in llm:
                print(f"⚠️ [models] {slot} 引用了不存在的 llm.ini section: {section}", file=sys.stderr)
                continue
            sec = llm[section]
            slots[slot] = ModelSlot(
                slot=slot,
                local_name=p.get("model_names", slot, fallback=_derive_local_name(section)).strip(),
                model_suffix=p.get("model_suffix", slot, fallback=default_model_suffix).strip(),
                section=section,
                base_url=sec["base_url"].rstrip("/"),
                api_key=sec["api_key"],
                upstream_model_id=sec["model"],
                protocol=sec.get("protocol", "openai").lower(),
                context_window=_context_window(p.get("context", slot, fallback=default_context_window)),
                supports_images=_parse_bool(sec.get("supports_images", "false")),
                daily_token_limit=(sec.get("daily_token_limit") or "").strip(),
            )

    _assign_groups_and_candidates(p, slots)

    return DccConfig(
        port=port,
        output_dir=output_dir,
        proxy_config_path=proxy_config_path,
        proxy_pidfile=proxy_pidfile,
        proxy_log=proxy_log,
        instances_file=instances_file,
        session_root=session_root,
        cc_binary=cc_binary,
        ready_timeout=p.getint("proxy", "ready_timeout", fallback=30),
        retention_days=p.getint("proxy", "retention_days", fallback=3),
        slots=slots,
        failover=failover,
    )


def _assign_groups_and_candidates(
    p: configparser.ConfigParser, slots: dict[str, ModelSlot]
) -> None:
    """解析 dcc.ini [groups] · 为每个 slot 填 group 名与 failover 候选列表。

    候选规则:同组 + 同 protocol(异协议不可互转,跳过) + 排除自己,
    倒序绕圈:从本槽前一个倒着走 · 回绕到组尾 → 主模型卡住时依次尝试。
    未配 [groups] 或 slot 不在任何组 → group="" 且 candidates=[](不参与自动切换)。
    """
    if not p.has_section("groups"):
        return
    default_keys = set(p.defaults().keys())
    # 组名 → 该组按声明顺序的 slot 号列表(仅保留 slots 里真实存在的)
    group_members: dict[str, list[str]] = {}
    for gname in p.options("groups"):
        if gname in default_keys:
            continue
        raw = p.get("groups", gname)
        members = [s.strip() for s in raw.split(",") if s.strip()]
        group_members[gname] = [s for s in members if s in slots]

    for gname, members in group_members.items():
        for idx, slot_id in enumerate(members):
            slot = slots[slot_id]
            slot.group = gname
            # 倒序绕圈:本槽之前成员逆序 + 回绕到本槽之后成员逆序 · 过滤同协议 · 排除自己
            ordered = members[:idx][::-1] + members[idx + 1:][::-1]
            slot.candidates = [
                slots[s].local_name
                for s in ordered
                if s != slot_id and slots[s].protocol == slot.protocol
            ]



def _context_window(value: str) -> int | None:
    value = value.strip().lower().replace("_", "")
    if not value:
        return None
    suffix = 1_000_000 if value.endswith("m") else 1_000
    number = value[:-1] if value[-1:] in ("k", "m") else value
    try:
        return int(float(number) * suffix)
    except ValueError:
        print(f"⚠️ context_window 无法解析: {value}", file=sys.stderr)
        return None


def _parse_bool(v: str | bool | None) -> bool:
    """通用 bool 解析 · 兼容 llm.ini 里手写的 true/1/yes/on。"""
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in ("true", "1", "yes", "on")


def _derive_local_name(section: str) -> str:
    """section 去掉 oxygen- 前缀作为 cc 端本地模型名(rag- 前缀保留,标识入口)。

    命名约定:llm-gw 入口 section = oxygen-*(去前缀后不带 rag);rag 入口 section =
    oxygen-rag-* 或 rag-*(去 oxygen- 后保留 rag- 前缀,与 llm-gw 区分)。
      oxygen-deepseek-v4-pro → deepseek-v4-pro      (llm-gw · 不带前缀)
      oxygen-rag-gpt-5.5    → rag-gpt-5.5           (rag · 带 rag-)
      rag-glm-5-1           → rag-glm-5-1           (rag · 带 rag-)
    """
    if section.startswith("oxygen-"):
        return section[len("oxygen-"):]
    return section


def _load_llm_ini(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        print(f"❌ llm.ini 不存在: {path}", file=sys.stderr)
        sys.exit(2)
    p = configparser.ConfigParser()
    p.read(path)
    out: dict[str, dict[str, str]] = {}
    for sec in p.sections():
        out[sec] = {k: p.get(sec, k) for k in p.options(sec) if k not in p.defaults()}
    return out
