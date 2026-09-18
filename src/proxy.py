"""dcc 自持轻代理:Anthropic /v1/messages ↔ 京东网关 OpenAI/Anthropic/Responses。

- 零第三方依赖(stdlib + requests · requests aqa 已依赖)
- SSE 双向转换:OpenAI chat.completions.chunk → Anthropic content_block_delta events
- 简单模型路由:model 匹配 dcc.ini [models] 的 <local_name>,按 protocol 走 openai/anthropic/responses
- 单文件 · 用 http.server.ThreadingHTTPServer,后台起,pidfile 记 PID
"""
from __future__ import annotations

import signal
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import DccConfig
from . import log as L


def is_port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def read_pid(pidfile: Path) -> int | None:
    if not pidfile.exists():
        return None
    try:
        pid = int(pidfile.read_text().strip())
    except (ValueError, OSError):
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def _trim_proxy_log(log_path: Path, keep: int = 200) -> None:
    """启动时把 dcc-proxy.log 裁到最后 keep 行 · 子进程接管句柄前做一次。

    dcc-proxy.log 由 proxy 子进程 stdout/stderr 独占句柄,运行期无法外部滚动,
    故只在此处(父进程 append 打开前)裁剪一次,语义同 log/exec_log/dumps 的启动清理。
    """
    try:
        if not log_path.exists():
            return
        with log_path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        if len(lines) <= keep:
            return
        with log_path.open("w", encoding="utf-8") as f:
            f.writelines(lines[-keep:])
    except OSError:
        pass


def start(cfg: DccConfig) -> int:
    """后台起代理子进程 · 通过 python -m src.proxy_server 入口。"""
    # 1. 探测可用端口 (如果配置端口被非本代理占用, 则偏移)
    original_port = cfg.port
    max_retries = 10
    for i in range(max_retries):
        current_port = original_port + i
        if not is_port_open(current_port):
            cfg.port = current_port
            break
        if _is_dcc_proxy(current_port):
            cfg.port = current_port
            L.log(f"dcc-proxy 已在跑 port={cfg.port}, 复用")
            # 校验 pidfile 是否一致, 不一致则更新(容错)
            existing_pid = read_pid(cfg.proxy_pidfile)
            if not existing_pid:
                L.log(f"[WARN] port={cfg.port} 有效但无 pidfile, 外部恢复", level="WARN")
            return existing_pid or -1
        L.log(f"[WARN] port={current_port} 被占用(非 dcc), 尝试偏移...", level="WARN")
    else:
        L.log(f"[ERROR] 端口 {original_port} 及其后 {max_retries} 个端口均不可用", level="ERROR")
        raise SystemExit(5)

    # 2. 只有确定了最终端口后才渲染配置
    render_config_yaml(cfg)

    server_py = Path(__file__).parent / "proxy_server.py"
    _trim_proxy_log(cfg.proxy_log)
    logf = cfg.proxy_log.open("ab")
    cmd = [sys.executable, str(server_py), str(cfg.proxy_config_path)]
    L.log(f"启动 dcc-proxy: {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        stdout=logf,
        stderr=logf,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    cfg.proxy_pidfile.write_text(str(proc.pid))
    L.log(f"proxy 主进程 pid={proc.pid},等待端口就绪…")

    if not _wait_ready(cfg):
        L.log("[ERROR] proxy 未在 ready_timeout 内就绪,查看 log:", level="ERROR")
        L.log(f"  tail -f {cfg.proxy_log}")
        raise SystemExit(4)

    L.log(f"proxy 就绪 · pid={proc.pid} · port={cfg.port}")
    return proc.pid


def _is_dcc_proxy(port: int) -> bool:
    """通过 /health 接口特征判断是否为 dcc 代理。"""
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=1) as r:
            data = json.loads(r.read().decode("utf-8"))
            # dcc 响应特征: {"status": "ok"}
            return data.get("status") == "ok"
    except Exception:
        return False


def _wait_ready(cfg: DccConfig) -> bool:
    deadline = time.time() + cfg.ready_timeout
    while time.time() < deadline:
        if is_port_open(cfg.port) and _health_ok(cfg.port):
            return True
        time.sleep(0.3)
    return False


def _health_ok(port: int) -> bool:
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.status < 400
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return False


def stop_proxy(cfg: DccConfig) -> bool:
    """kill dcc 代理进程 · 返回 True=已杀,False=不在/已死。"""
    pid = read_pid(cfg.proxy_pidfile)
    if not pid:
        L.log("dcc-proxy 已不在运行(无 pidfile)")
        return False
    L.log(f"停止 dcc-proxy pid={pid}")
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.3)
        if _pid_alive(pid):
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.3)
        cfg.proxy_pidfile.unlink(missing_ok=True)
    except OSError as e:
        L.log(f"[WARN] kill 代理失败: {e}", level="WARN")
        return False
    return True


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def status(cfg: DccConfig) -> dict:
    pid = read_pid(cfg.proxy_pidfile)
    port_open = is_port_open(cfg.port)
    healthy = _health_ok(cfg.port) if port_open else False
    return {
        "pid": pid,
        "port": cfg.port,
        "port_open": port_open,
        "healthy": healthy,
        "config": str(cfg.proxy_config_path),
        "log": str(cfg.proxy_log),
    }


def render_config_yaml(cfg: DccConfig) -> None:
    """把 dcc.ini [models] + llm.ini 落成 config.json(每模型自带三要素)。

    schema:
      {port, models: [{name, protocol, base_url, api_key, upstream_id, slot}]}
    """
    data = {
        "port": cfg.port,
        "retention_days": cfg.retention_days,
        "failover": {
            "soft_timeout": cfg.failover.soft_timeout,
            "switch_on_429": cfg.failover.switch_on_429,
            "max_candidates": cfg.failover.max_candidates,
        },
        "models": [
            {
                "name": s.local_name,
                "section": s.section,
                "protocol": s.protocol,
                "base_url": s.base_url,
                "api_key": s.api_key,
                "upstream_id": s.upstream_model_id,
                "slot": s.slot,
                "supports_images": s.supports_images,
                "group": s.group,
                "candidates": s.candidates,
            }
            for _slot_id, s in sorted(cfg.slots.items())
        ],
    }
    cfg.proxy_config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    L.log(f"渲染 proxy config → {cfg.proxy_config_path} · models={len(cfg.slots)}")
