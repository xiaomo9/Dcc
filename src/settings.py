"""将全部 dcc 槽位合并到 Claude 用户设置。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from .config import DccConfig


def write(cfg: DccConfig, path: Path) -> bool:
    slots = list(cfg.slots.values())
    if not slots:
        raise ValueError("未配置有效槽位，请检查 dcc.ini [models]")
    path = path.expanduser().resolve()
    try:
        original = path.read_bytes()
    except FileNotFoundError:
        original = None
    existing = json.loads(original) if original is not None else {}
    if not isinstance(existing, dict) or not isinstance(existing.get("env", {}), dict):
        raise ValueError("settings.json 必须是 JSON 对象，env 必须是对象")
    old_env = existing.get("env", {})
    headers = old_env.get("ANTHROPIC_CUSTOM_HEADERS", "")
    if not isinstance(headers, str):
        raise ValueError("ANTHROPIC_CUSTOM_HEADERS 必须是字符串")
    headers = [line for line in headers.splitlines()
               if line.partition(":")[0].strip().lower() != "x-dcc-client"]
    headers.append("x-dcc-client: vscode")
    default = f"dcc-slot-{slots[0].slot}"
    env = {**old_env,
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{cfg.port}",
        "ANTHROPIC_AUTH_TOKEN": "dummy",
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_MODEL": default,
        "ANTHROPIC_DEFAULT_MODEL": default,
        "ANTHROPIC_CUSTOM_HEADERS": "\n".join(headers),
        "CLAUDE_CODE_USE_BEDROCK": "0",
        "CLAUDE_CODE_USE_VERTEX": "0",
        "CLAUDE_CODE_USE_FOUNDRY": "0",
    }
    for family in ("SONNET", "OPUS", "HAIKU"):
        env[f"ANTHROPIC_DEFAULT_{family}_MODEL"] = default
        env[f"ANTHROPIC_DEFAULT_{family}_MODEL_NAME"] = default
    data = {**existing, "env": env, "model": default, "modelPicker": {
        "replaceBuiltInOptions": True,
        "options": [{"model": f"dcc-slot-{slot.slot}",
                     "label": f"#{slot.slot} · {slot.local_name}"} for slot in slots],
    }}
    if data == existing:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", delete=False) as f:
            tmp = Path(f.name)
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    return True
