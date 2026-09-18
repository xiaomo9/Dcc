"""dcc-proxy 服务器主进程:Anthropic /v1/messages → 京东网关 OpenAI/Anthropic/Responses。

用法(dcc 内部调):
    python3 proxy_server.py <config.json>

config.json(由 src/proxy.py::render_config_yaml 写):
    {
      "port": 4000,
      "models": [
        {
          "name": "glm-5-2",
          "section": "oxygen-glm-5-2",
          "protocol": "openai",
          "base_url": "http://llm-gw.jd.local/v1",
          "api_key": "<gateway token>",
          "upstream_id": "GLM-5.2-joybuilder",
          "slot": "2"
        }
      ]
    }

每个模型自持三要素(base_url + api_key + upstream_id)· 来自 ~/.dm/llm.ini 单点权威。
协议分发靠 model.protocol · openai / anthropic / responses。

协议转换范围
- 请求:Anthropic messages → OpenAI messages(role/content 平铺,system 拆到最前)
- 响应:OpenAI chat.completions.chunk SSE → Anthropic content_block_delta SSE
- 工具:tools/tool_use/tool_result 双向转换(openai tool_calls / anthropic 原生透传)
- 非流式:走 stream=true 内部聚合(cc 端不区分,反正它要 SSE)

单文件 · 无 pip 依赖 · 用 requests(aqa 已依赖)+ stdlib http.server。
"""
from __future__ import annotations

import http.server
import json
import os
import queue
import socketserver
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

import requests
import urllib3

from traffic_log import ExecLog, TrafficLog
from retry_policy import compute_backoff, format_upstream_error, is_retryable_status, MAX_RETRIES


# 版本号统一从 VERSION 读取 · P.start 用脚本直接运行方式, 不能走相对导入, 直接读文件
def _dcc_version() -> str:
    vf = Path(__file__).parent / "VERSION"
    try:
        return vf.read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"
DCC_VERSION = _dcc_version()

# 请求原始 body 落盘目录 · 遇到 400 时保留最近 N 份 request/response · 便于离线复现
DUMP_DIR: Path | None = None
DUMP_KEEP = 20

# 全流量日志(cc↔proxy↔网关 上下行)· 按天 + 10MB 拆分 · daemon 线程异步刷盘 · 不阻塞主链路
TL: TrafficLog | None = None
# 执行日志(控制流 · 精简)· 按天拆分 · 与 TrafficLog 分开(TL 记会话体量大 · EL 只记控制流)
EL: ExecLog | None = None

# 流式中断保护 · DeepSeek-V4/Qwen 推理链路长上下文下网关偶发 ChunkedEncoding 断流
# · 首帧到达前允许重试(重放安全)· 首帧后合法闭合已开块并向 CC 端 emit error,防"响应中断"
STREAM_MAX_RETRIES = int(os.environ.get("DCC_STREAM_RETRIES", "1"))
# 上游连接超时 / socket 静默(两字节之间)超时 · requests.timeout=(connect, read) 的 read 即静默
# 单请求总时长仍受 requests + 上游共同约束
STREAM_CONNECT_TIMEOUT = float(os.environ.get("DCC_STREAM_CONNECT_TIMEOUT", "10"))
STREAM_IDLE_TIMEOUT = float(os.environ.get("DCC_STREAM_IDLE_TIMEOUT", "90"))

# 触发首帧前重试或保护关闭的异常集合 · 都属"流式中断/网关波动"类
_STREAM_RETRY_EXC = (
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ConnectionError,
    requests.exceptions.ReadTimeout,
    urllib3.exceptions.ProtocolError,
    urllib3.exceptions.ReadTimeoutError,
    urllib3.exceptions.IncompleteRead,
)


# reasoning 三态开关:
# - "hide"(默认):走 _strip_think_stream 过滤 <think>...</think> · reasoning_content 也丢
# - "text":合并到普通 text_delta(旧 DCC_SHOW_REASONING=1 行为)
# - "thinking":发独立 anthropic thinking block(content_block_delta.thinking_delta)· cc 端能折叠
def _reasoning_mode() -> str:
    m = (os.environ.get("DCC_REASONING_MODE") or "").lower().strip()
    if m in ("hide", "text", "thinking"):
        return m
    # 向后兼容旧开关
    if os.environ.get("DCC_SHOW_REASONING") == "1":
        return "text"
    return "hide"


def _is_stream_break(exc: BaseException) -> bool:
    """判定是否属于流式中断类异常(用于选择重试/保护关闭路径)。"""
    return isinstance(exc, _STREAM_RETRY_EXC)


# ============================================================
# 首字节守卫 · 组内 failover 支撑
# ============================================================
# 硬约束:上游一旦吐出首字节即锁定,后续绝不切换(换模型会污染已输出内容)。
# 切换窗口只在首字节前,故需"首帧前 soft_timeout / 首帧后放宽到 idle"两段超时。
# requests 的 (connect, read) 超时全程固定、中途改不了,单线程做不到两段。
# 方案:后台 daemon 线程只负责"连接 + 取第一个 SSE 行",主线程 queue.get(timeout)
# 限时等首行;拿到后把 (response, 首行, 行迭代器) 交回主线程原读循环继续跑。
# 后台线程从不写 client socket,写全在主线程锁定候选后进行,无并发写冲突。

class _SoftTimeout(Exception):
    """首字节在 soft_timeout 秒内未到达 · 判定主模型慢 · 切下一候选。"""


class _Upstream429(Exception):
    """首帧前上游返回可重试状态(429/5xx) · 供 failover 决定切换或退避。"""

    def __init__(self, status: int, body_preview: str, retry_after: str | None):
        super().__init__(f"upstream {status}")
        self.status = status
        self.body_preview = body_preview
        self.retry_after = retry_after


class _GuardedConn:
    """守卫连接的产物:锁定的上游 response + 已取出的首行 + 剩余行迭代器。

    first_line 是拿到的第一个非空 SSE 行(bytes 或 str · 取决于 decode_unicode)。
    调用方应先处理 first_line,再继续迭代 rest 直到耗尽。iter_all() 把两者串起来。
    """

    def __init__(self, response, first_line, rest_iter):
        self.response = response
        self.first_line = first_line
        self._rest = rest_iter

    def iter_all(self):
        yield self.first_line
        yield from self._rest


def _guarded_connect(
    url: str, payload: dict, headers: dict, *, soft_timeout: float,
    decode_unicode: bool, req_id: str, protocol: str,
) -> _GuardedConn:
    """连上游并在 soft_timeout 内等首个 SSE 行。

    成功 → 返回 _GuardedConn(response 保持打开 · 由调用方负责 close)。
    首字节超时 → 关连接抛 _SoftTimeout。
    首帧前遇上游 >=400 → 关连接抛 _Upstream429(带 status/body · 交调用方按可重试性决定切/退避/报错)。
    首帧前连接异常 → 原样向上抛(由调用方现有逻辑处理)。
    """
    result_q: queue.Queue = queue.Queue(maxsize=1)

    def _worker():
        try:
            r = requests.post(
                url, json=payload, headers=headers, stream=True,
                timeout=(STREAM_CONNECT_TIMEOUT, STREAM_IDLE_TIMEOUT),
            )
            if r.status_code >= 400:
                body_preview = r.text[:1500]
                retry_after = r.headers.get("Retry-After")
                r.close()
                result_q.put(("http_error", (r.status_code, body_preview, retry_after)))
                return
            r.encoding = "utf-8"
            line_iter = r.iter_lines(decode_unicode=decode_unicode, chunk_size=1)
            empty = "" if decode_unicode else b""
            first = None
            for line in line_iter:
                if line == empty or not line:
                    continue
                first = line
                break
            if first is None:
                # 上游没吐任何非空行就结束了(空响应)· 交回让读循环正常收尾
                result_q.put(("ok", (r, empty, iter(()))))
                return
            result_q.put(("ok", (r, first, line_iter)))
        except BaseException as e:  # noqa: BLE001 · 连接/读异常原样带回主线程判类型
            result_q.put(("exc", e))

    t = threading.Thread(target=_worker, name=f"guard-{req_id}", daemon=True)
    t.start()
    try:
        kind, val = result_q.get(timeout=soft_timeout)
    except queue.Empty:
        _log(f"[WARN] {protocol} 首字节 {soft_timeout:.0f}s 未到达 · req_id={req_id} · 判定慢")
        raise _SoftTimeout()
    if kind == "ok":
        r, first, rest = val
        return _GuardedConn(r, first, rest)
    if kind == "http_error":
        status, body_preview, retry_after = val
        _log(f"[WARN] {protocol} upstream {status} (首帧前) · req_id={req_id} · body={body_preview[:300]}")
        raise _Upstream429(status, body_preview, retry_after)
    # kind == "exc"
    raise val


class Config:
    def __init__(self, data: dict):
        self.port = int(data["port"])
        self.models_by_name: dict[str, dict] = {m["name"]: m for m in data.get("models", [])}
        self._default: dict | None = data["models"][0] if data.get("models") else None
        fo = data.get("failover") or {}
        self.soft_timeout: float = float(fo.get("soft_timeout", 12.0))
        self.switch_on_429: bool = bool(fo.get("switch_on_429", True))
        self.max_candidates: int = int(fo.get("max_candidates", 3))

    def resolve(self, name: str) -> dict | None:
        if name in self.models_by_name:
            return self.models_by_name[name]
        # Claude Code 的模型后缀是上下文标记,不属于上游模型名。
        for base_name in self.models_by_name:
            if name.startswith(base_name) and name[len(base_name):].startswith("["):
                _log(f"[INFO] model={name} · 去除上下文标记后路由={base_name}")
                return self.models_by_name[base_name]
        # 兜底:cc 端内部子任务或误传 · 路由到默认 slot 避免 404 中断
        if self._default:
            _log(f"[WARN] 未知 model={name} · 兜底路由到 default={self._default['name']}")
            return self._default
        return None

    def resolve_candidates(self, name: str) -> list[dict]:
        """返回 [主模型, 候选1, 候选2...] 的 model dict 列表(截断到 max_candidates)。

        主模型走 resolve();候选取主模型 dict 里的 candidates(同组同协议 local_name),
        逐个查 models_by_name。用于首字节超时/429 时的组内 failover。
        """
        primary = self.resolve(name)
        if not primary:
            return []
        chain = [primary]
        for cand_name in primary.get("candidates", []):
            m = self.models_by_name.get(cand_name)
            if m is not None:
                chain.append(m)
            if len(chain) >= self.max_candidates:
                break
        return chain


CONFIG: Config


def _log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] [dcc-proxy] {msg}"
    print(line, flush=True)
    if EL is not None:
        EL.log(f"[dcc-proxy] {msg}")


def _tl(kind: str, **fields) -> None:
    """内部转发到全局 TrafficLog · 未初始化时短路,不影响主链路。"""
    if TL is not None:
        TL.log(kind, **fields)


def _strip_think_stream(chunk: str, state: dict) -> str:
    """流式剔除 <think>...</think> 块 · 跨 chunk 边界安全。

    返回:visible_text(<think> 外的正文)· 兼容旧接口。
    需要分开取 thinking 段用 _split_think_stream。
    """
    _thinking, visible = _split_think_stream(chunk, state)
    return visible


def _split_think_stream(chunk: str, state: dict) -> tuple[str, str]:
    """流式拆 <think>...</think> · 返回 (thinking_text, visible_text)。

    state = {"in_think": bool, "buf": str, "mode": None | "tag" | "prefix", "think_buf": str}

    首个 chunk 探测决定 mode:
    - "prefix":首个 chunk 里带 </think> · 模型开头就吐思考没开标签
              → 从头累积到首个 </think>,前段全部当 thinking · 剩余走 tag 模式
    - "tag":<think>...</think> 完整标签形式 · 状态机拆
    """
    if not chunk:
        return "", ""
    mode = state.get("mode")
    if mode is None:
        close_idx = chunk.find("</think>")
        open_idx = chunk.find("<think>")
        if close_idx != -1 and (open_idx == -1 or close_idx < open_idx):
            state["mode"] = "prefix"
        else:
            state["mode"] = "tag"

    if state["mode"] == "prefix":
        return _split_prefix_think(chunk, state)
    return _split_think_tags(chunk, state)


def _split_prefix_think(chunk: str, state: dict) -> tuple[str, str]:
    """prefix 模式:开头无 <think> 只有 </think> · 之前全当 thinking。"""
    state["buf"] = state.get("buf", "") + chunk
    idx = state["buf"].find("</think>")
    if idx == -1:
        # 未见闭标签 · 全部当 thinking 累积
        thinking = state["buf"]
        state["buf"] = ""
        return thinking, ""
    thinking = state["buf"][:idx]
    remain = state["buf"][idx + 8:]
    state["buf"] = ""
    state["mode"] = "tag"
    t2, v = _split_think_tags(remain, state)
    return thinking + t2, v


def _split_think_tags(chunk: str, state: dict) -> tuple[str, str]:
    """tag 模式:in_think 段累积到 thinking,其他到 visible。末尾 hold 防 tag 跨 chunk。"""
    if not chunk:
        return "", ""
    text = state.get("buf", "") + chunk
    state["buf"] = ""
    thinking: list[str] = []
    visible: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if state.get("in_think"):
            end = text.find("</think>", i)
            if end == -1:
                # 剩余全在 think · 但末尾保留 7 字防 </think> 跨 chunk
                keep = max(i, n - 7)
                thinking.append(text[i:keep])
                state["buf"] = text[keep:]
                return "".join(thinking), "".join(visible)
            thinking.append(text[i:end])
            i = end + 8
            state["in_think"] = False
        else:
            start = text.find("<think>", i)
            if start == -1:
                keep = max(i, n - 6)
                visible.append(text[i:keep])
                state["buf"] = text[keep:]
                return "".join(thinking), "".join(visible)
            visible.append(text[i:start])
            i = start + 7
            state["in_think"] = True
    return "".join(thinking), "".join(visible)


def _dump_dir_ensure() -> Path | None:
    if DUMP_DIR is None:
        return None
    DUMP_DIR.mkdir(parents=True, exist_ok=True)
    return DUMP_DIR


def _dump_request(upstream_id: str, body_original: dict, body_upstream: dict, response_preview: str) -> Path | None:
    """把请求原始 + 上游 payload + 响应片段落盘 · 遇 400 时保 20 份滚动。"""
    d = _dump_dir_ensure()
    if d is None:
        return None
    ts = time.strftime("%Y%m%d_%H%M%S")
    ms = int((time.time() % 1) * 1000)
    safe_id = upstream_id.replace("/", "_").replace(":", "_")
    path = d / f"req_{ts}_{ms:03d}_{safe_id}.json"
    try:
        path.write_text(
            json.dumps(
                {
                    "upstream_id": upstream_id,
                    "anthropic_body": body_original,
                    "upstream_payload": body_upstream,
                    "response_preview": response_preview,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as e:
        _log(f"[WARN] dump 写盘失败: {e}")
        return None
    try:
        reqs = sorted(d.glob("req_*.json"))
        for old in reqs[:-DUMP_KEEP]:
            old.unlink(missing_ok=True)
    except OSError:
        pass
    return path


# ============================================================
# Anthropic → OpenAI 消息转换
# ============================================================

def _strip_images_if_unsupported(body: dict, model: dict) -> int:
    """当 model.supports_images 为 false 时 · 原地剥掉 body.messages 里所有 image / image_url 块。

    覆盖三种位置:
      1. content=list 里 type=image 的 block(Anthropic 原生)
      2. content=list 里 type=image_url 的 part(openai 风格 · CC 不会主动发但兜底)
      3. tool_result.content 内嵌 image block(工具返图 · 比如截图工具)

    返回剥离的图片数量。0 = 无图或模型支持图。原地修改 body,后续 transform 看不到 image。
    """
    if model.get("supports_images"):
        return 0
    stripped = 0
    for m in body.get("messages", []) or []:
        c = m.get("content")
        if not isinstance(c, list):
            continue
        kept: list = []
        for blk in c:
            if not isinstance(blk, dict):
                kept.append(blk)
                continue
            btype = blk.get("type")
            if btype in ("image", "image_url"):
                stripped += 1
                continue
            if btype == "tool_result":
                inner = blk.get("content")
                if isinstance(inner, list):
                    inner_kept = []
                    for ib in inner:
                        if isinstance(ib, dict) and ib.get("type") in ("image", "image_url"):
                            stripped += 1
                            continue
                        inner_kept.append(ib)
                    if inner_kept != inner:
                        blk = {**blk, "content": inner_kept}
            kept.append(blk)
        if kept != c:
            m["content"] = kept
    return stripped


def anthropic_to_openai_messages(body: dict) -> tuple[list[dict], dict]:
    """把 Anthropic /v1/messages 请求 body 转成 OpenAI chat.completions payload 骨架。

    tool_use / tool_result → OpenAI tool_calls / role=tool 消息(完整双向)。
    返回 (openai_messages, extra)· extra 含 tools / max_tokens / temperature 等透传字段。
    """
    system_val = body.get("system")
    system_texts: list[str] = []
    if isinstance(system_val, str) and system_val.strip():
        system_texts.append(system_val)
    elif isinstance(system_val, list):
        for blk in system_val:
            if isinstance(blk, dict) and blk.get("type") == "text":
                t = blk.get("text", "")
                if t:
                    system_texts.append(t)

    # 先扫一遍 body.messages 里的 role=system(claude CLI 会把 SessionStart hook 等塞进来)
    # · 京东 Qwen 网关严格要求 "System message must be at the beginning" · 需把所有 system 合并前置
    for m in body.get("messages", []):
        if m.get("role") == "system":
            c = m.get("content", "")
            if isinstance(c, str) and c.strip():
                system_texts.append(c)
            elif isinstance(c, list):
                for blk in c:
                    if isinstance(blk, dict) and blk.get("type") == "text" and blk.get("text"):
                        system_texts.append(blk["text"])

    messages: list[dict] = []
    if system_texts:
        messages.append({"role": "system", "content": "\n\n".join(system_texts)})

    for m in body.get("messages", []):
        role = m.get("role", "user")
        if role == "system":
            continue  # 已在开头合并,跳过
        content = m.get("content", "")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        # blocks list · 四类:text / image / tool_use(assistant 发的调用) / tool_result(user 发的结果)
        text_parts: list[str] = []
        image_parts: list[dict] = []
        tool_calls: list[dict] = []
        tool_results: list[tuple[str, list]] = []  # (tool_use_id, content_blocks)
        for blk in content:
            if not isinstance(blk, dict):
                continue
            btype = blk.get("type")
            if btype == "text":
                text_parts.append(blk.get("text", ""))
            elif btype == "image":
                url = _anthropic_image_to_openai_url(blk.get("source") or {})
                if url:
                    image_parts.append({"type": "image_url", "image_url": {"url": url}})
            elif btype == "tool_use":
                tool_calls.append({
                    "id": blk.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": blk.get("name", ""),
                        "arguments": json.dumps(blk.get("input", {}), ensure_ascii=False),
                    },
                })
            elif btype == "tool_result":
                # tool_result 内 content 可为 str / list(含 text + image) · 收原始 blocks 后再拆
                raw = blk.get("content", "")
                if isinstance(raw, list):
                    blocks = raw
                elif isinstance(raw, str):
                    blocks = [{"type": "text", "text": raw}]
                else:
                    blocks = [{"type": "text", "text": json.dumps(raw, ensure_ascii=False)}]
                tool_results.append((blk.get("tool_use_id", ""), blocks))
            else:
                text_parts.append(f"[{btype}]")

        # role=user 里的 tool_result 拆成独立 role=tool 消息(每个 tool_use_id 一条)
        for tid, blocks in tool_results:
            messages.append({
                "role": "tool",
                "tool_call_id": tid,
                "content": _tool_result_blocks_to_openai(blocks),
            })

        # 剩余 text / image / tool_use 组成一条 role=user 或 role=assistant 消息
        msg: dict = {"role": role}
        text_str = "\n".join(t for t in text_parts if t)
        if role == "assistant" and tool_calls:
            msg["content"] = text_str or None
            msg["tool_calls"] = tool_calls
        elif image_parts:
            # user 侧带图片 · content 必须是 list 形态(openai vision 契约)
            parts: list[dict] = []
            if text_str:
                parts.append({"type": "text", "text": text_str})
            parts.extend(image_parts)
            msg["content"] = parts
        else:
            if text_str:
                msg["content"] = text_str
            elif not tool_results:
                # 空消息给个空 content 避免 openai 400
                msg["content"] = ""
            else:
                continue  # 只是 tool_result,跳过 role=user 空消息(已放到 role=tool 里)
        messages.append(msg)

    extra = {
        "max_tokens": body.get("max_tokens"),
        "temperature": body.get("temperature"),
        "top_p": body.get("top_p"),
        "stop": body.get("stop_sequences"),
    }
    # tools 字段:anthropic → openai(name/description/input_schema → function 结构)
    if body.get("tools"):
        extra["tools"] = _anthropic_tools_to_openai(body["tools"])
    if body.get("tool_choice"):
        extra["tool_choice"] = _anthropic_tool_choice_to_openai(body["tool_choice"])
    extra = {k: v for k, v in extra.items() if v is not None}
    return messages, extra


def _anthropic_image_to_openai_url(source: dict) -> str | None:
    """把 Anthropic image block 的 source 转成 OpenAI data URL 或直接 url。

    Anthropic 支持 source.type = "base64"(带 media_type + data)或 "url"(直接 URL)。
    OpenAI vision 都吃 image_url.url · 直接 URL 或 data:<mime>;base64,<...>。
    """
    if not isinstance(source, dict):
        return None
    stype = source.get("type")
    if stype == "url":
        u = source.get("url")
        return u if isinstance(u, str) and u else None
    if stype == "base64":
        media = source.get("media_type") or "image/png"
        data = source.get("data")
        if isinstance(data, str) and data:
            return f"data:{media};base64,{data}"
    return None


def _tool_result_blocks_to_openai(blocks: list) -> str | list:
    """tool_result.content(anthropic 允许 text+image 混排)转 openai role=tool.content。

    - 纯 text → str(向后兼容 · 京东网关友好)
    - 含 image → list of parts(openai role=tool 支持 vision parts:2024 上半年才通)
    """
    text_parts: list[str] = []
    image_parts: list[dict] = []
    for b in blocks:
        if not isinstance(b, dict):
            text_parts.append(str(b))
            continue
        btype = b.get("type")
        if btype == "text":
            t = b.get("text", "")
            if t:
                text_parts.append(t)
        elif btype == "image":
            url = _anthropic_image_to_openai_url(b.get("source") or {})
            if url:
                image_parts.append({"type": "image_url", "image_url": {"url": url}})
        else:
            text_parts.append(json.dumps(b, ensure_ascii=False))
    text_str = "\n".join(t for t in text_parts if t)
    if not image_parts:
        return text_str
    parts: list[dict] = []
    if text_str:
        parts.append({"type": "text", "text": text_str})
    parts.extend(image_parts)
    return parts


def _anthropic_tools_to_openai(tools: list[dict]) -> list[dict]:
    out = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        schema = t.get("input_schema") or {}
        if not isinstance(schema, dict) or not schema:
            schema = {"type": "object", "properties": {}}
        if schema.get("type") == "object" and not schema.get("properties"):
            schema = {**schema, "properties": {}}
        out.append({
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": schema,
            },
        })
    return out


def _anthropic_tool_choice_to_openai(tc: dict) -> str | dict:
    ttype = tc.get("type") if isinstance(tc, dict) else None
    if ttype == "auto":
        return "auto"
    if ttype == "any":
        return "required"
    if ttype == "tool":
        return {"type": "function", "function": {"name": tc.get("name", "")}}
    return "auto"


def _openai_tools_to_responses(tools: list[dict]) -> list[dict]:
    """OpenAI function tool 结构转 Responses API function tool 结构。"""
    out = []
    for tool in tools:
        fn = tool.get("function") or {}
        if tool.get("type") != "function" or not fn.get("name"):
            continue
        out.append({
            "type": "function",
            "name": fn["name"],
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def _openai_messages_to_responses_input(messages: list[dict]) -> list[dict]:
    """把已规范化的消息转为 Responses 的 function_call 输入项。"""
    tool_ids: dict[str, tuple[str, str]] = {}
    for message in messages:
        for tool_call in message.get("tool_calls") or []:
            original_id = tool_call.get("id", "")
            suffix = original_id.removeprefix("toolu_") or uuid.uuid4().hex[:16]
            response_id = original_id if original_id.startswith("fc_") else f"fc_{suffix}"
            call_id = original_id if original_id.startswith("call_") else f"call_{suffix}"
            tool_ids[original_id] = (response_id, call_id)

    out = []
    for message in messages:
        role = message.get("role")
        if role == "tool":
            original_id = message.get("tool_call_id", "")
            _response_id, call_id = tool_ids.get(original_id, ("", original_id))
            out.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": _openai_content_to_responses_output(message.get("content", "")),
            })
            continue
        tool_calls = message.get("tool_calls") or []
        if role == "assistant" and tool_calls:
            if message.get("content"):
                out.append({"role": "assistant", "content": _openai_content_to_responses_parts(message["content"], role="assistant")})
            for tool_call in tool_calls:
                fn = tool_call.get("function") or {}
                response_id, call_id = tool_ids.get(tool_call.get("id", ""), ("", ""))
                out.append({
                    "type": "function_call",
                    "id": response_id,
                    "call_id": call_id,
                    "name": fn.get("name", ""),
                    "arguments": fn.get("arguments", "{}"),
                })
            continue
        # 常规 user/assistant 消息 · content 可能是 str 或 openai vision list
        parts = _openai_content_to_responses_parts(message.get("content"), role=role or "user")
        out.append({"role": role, "content": parts})
    return out


def _openai_content_to_responses_parts(content, *, role: str) -> list[dict]:
    """OpenAI 消息 content(str 或 vision list)转 Responses input parts。

    input_text / input_image(user)· output_text(assistant)
    """
    text_type = "output_text" if role == "assistant" else "input_text"
    if content is None:
        return [{"type": text_type, "text": ""}]
    if isinstance(content, str):
        return [{"type": text_type, "text": content}]
    parts: list[dict] = []
    if isinstance(content, list):
        for p in content:
            if not isinstance(p, dict):
                parts.append({"type": text_type, "text": str(p)})
                continue
            ptype = p.get("type")
            if ptype == "text":
                parts.append({"type": text_type, "text": p.get("text", "")})
            elif ptype == "image_url":
                url = (p.get("image_url") or {}).get("url", "")
                if url:
                    parts.append({"type": "input_image", "image_url": url})
    if not parts:
        parts.append({"type": text_type, "text": ""})
    return parts


def _openai_content_to_responses_output(content) -> str:
    """tool_result 侧:function_call_output.output 是 str · 图片降级为占位。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        buf: list[str] = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") == "text":
                    buf.append(p.get("text", ""))
                elif p.get("type") == "image_url":
                    buf.append("[image]")
        return "\n".join(b for b in buf if b)
    return str(content or "")


# ============================================================
# Anthropic SSE 事件构造(cc 期望的流式格式)
# ============================================================

def sse_event(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def emit_message_start(msg_id: str, model: str, input_tokens: int | None = None) -> bytes:
    """初始化 message_start 事件。

    input_tokens 为 None 时:用 0(流式开始前不预先知道 input 字数)。
    传 int 时:用该值(如非流式请求/上游已返回 usage)。
    """
    return sse_event("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": input_tokens or 0, "output_tokens": 0},
        },
    })


def emit_content_block_start(idx: int) -> bytes:
    return sse_event("content_block_start", {
        "type": "content_block_start",
        "index": idx,
        "content_block": {"type": "text", "text": ""},
    })


def emit_tool_use_start(idx: int, tool_id: str, name: str) -> bytes:
    return sse_event("content_block_start", {
        "type": "content_block_start",
        "index": idx,
        "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}},
    })


def emit_thinking_block_start(idx: int) -> bytes:
    return sse_event("content_block_start", {
        "type": "content_block_start",
        "index": idx,
        "content_block": {"type": "thinking", "thinking": ""},
    })


def emit_thinking_delta(idx: int, text: str) -> bytes:
    return sse_event("content_block_delta", {
        "type": "content_block_delta",
        "index": idx,
        "delta": {"type": "thinking_delta", "thinking": text},
    })


def emit_tool_input_delta(idx: int, partial_json: str) -> bytes:
    return sse_event("content_block_delta", {
        "type": "content_block_delta",
        "index": idx,
        "delta": {"type": "input_json_delta", "partial_json": partial_json},
    })


def emit_content_delta(idx: int, text: str) -> bytes:
    return sse_event("content_block_delta", {
        "type": "content_block_delta",
        "index": idx,
        "delta": {"type": "text_delta", "text": text},
    })


def emit_content_block_stop(idx: int) -> bytes:
    return sse_event("content_block_stop", {
        "type": "content_block_stop",
        "index": idx,
    })


def emit_message_delta(stop_reason: str, usage: dict) -> bytes:
    """message_delta 事件 · usage 应为 dict 含 input_tokens + output_tokens。

    各协议路径在调用前需要把上游真实 token 计数填入 usage。
    """
    return sse_event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": usage,
    })


def emit_message_stop() -> bytes:
    return sse_event("message_stop", {"type": "message_stop"})


def emit_error(msg: str) -> bytes:
    return sse_event("error", {"type": "error", "error": {"type": "api_error", "message": msg}})


def _write_stripped_images_warning(handler: "ProxyHandler") -> bool:
    """如果本请求 dcc 剥了图 · 在 assistant 首帧前打开 text block 并写一段明文提示。

    返回 True = 已开 block(且写了 warning delta · 调用方跳过自己的 content_block_start(0));
    False = 无剥图 · 调用方正常开 block。
    """
    n = getattr(handler, "_dcc_stripped_imgs", 0) or 0
    if n <= 0:
        return False
    slot = getattr(handler, "_dcc_slot", "?")
    upstream = getattr(handler, "_dcc_upstream", "?")
    msg = (
        f"[dcc] ⚠️ 本 slot(slot={slot} · {upstream})不支持图文输入 · "
        f"已剥离 {n} 张历史图片 · 后续回答将忽略图片内容。\n"
        f"如需图文能力 · 换用 vision-capable slot 或在 ~/.dm/llm.ini 对应 section 设 `supports_images = true`(先确认模型确实支持)。\n\n"
    )
    handler.wfile.write(emit_content_block_start(0))
    handler.wfile.write(emit_content_delta(0, msg))
    handler.wfile.flush()
    return True


class _BufWFile:
    """替身 wfile · 让三条 stream_*_to_anthropic 在 stream=False 时把 SSE 写进内存,
    随后由 collect_sse_to_message 聚合成一个 anthropic Message JSON。
    只需支持 write/flush · handler.wfile 别的方法本模块没用到。
    """

    def __init__(self) -> None:
        self._buf: list[bytes] = []

    def write(self, data: bytes) -> int:
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._buf.append(data)
        return len(data)

    def flush(self) -> None:
        pass

    def getvalue(self) -> bytes:
        return b"".join(self._buf)


def _parse_sse_events(raw: bytes) -> list[tuple[str, dict]]:
    """把 emit_* 产出的 `event: X\\ndata: {...}\\n\\n` 切回 (event_name, data) 列表。
    不是 emit_* 出品的行(比如 message_start 前的空行)静默丢。
    """
    events: list[tuple[str, dict]] = []
    text = raw.decode("utf-8", errors="replace")
    for chunk in text.split("\n\n"):
        ev_name = ""
        data_str = ""
        for line in chunk.split("\n"):
            if line.startswith("event: "):
                ev_name = line[7:].strip()
            elif line.startswith("data: "):
                data_str = line[6:]
        if not ev_name or not data_str:
            continue
        try:
            events.append((ev_name, json.loads(data_str)))
        except json.JSONDecodeError:
            continue
    return events


def collect_sse_to_message(raw: bytes, fallback_model: str, fallback_msg_id: str) -> dict:
    """把一段完整的 anthropic SSE 流聚合成 anthropic /v1/messages 非流响应 body。
    - message_start.message 作骨架 · id/model/role/usage
    - content_block_start 建 block 骨架(text/thinking/tool_use)
    - content_block_delta 累加 text_delta / thinking_delta / input_json_delta(partial_json)
    - message_delta 覆盖 stop_reason/stop_sequence · usage.output_tokens 追加
    - tool_use.input 从 partial_json 拼接后 JSON.parse · 拼不出则回退 {}
    - 缺 message_start 时用 fallback_* 兜底(error 事件也走这里)
    """
    message: dict = {
        "id": fallback_msg_id,
        "type": "message",
        "role": "assistant",
        "model": fallback_model,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    blocks_by_idx: dict[int, dict] = {}
    tool_partial_json: dict[int, list[str]] = {}
    error_msg: str | None = None

    for ev_name, data in _parse_sse_events(raw):
        if ev_name == "message_start":
            m = data.get("message") or {}
            for k in ("id", "model", "role"):
                if m.get(k):
                    message[k] = m[k]
            usage = m.get("usage") or {}
            if usage:
                message["usage"]["input_tokens"] = int(usage.get("input_tokens") or 0)
                message["usage"]["output_tokens"] = int(usage.get("output_tokens") or 0)
        elif ev_name == "content_block_start":
            idx = int(data.get("index", 0))
            cb = dict(data.get("content_block") or {})
            btype = cb.get("type")
            if btype == "tool_use":
                cb.setdefault("input", {})
                tool_partial_json[idx] = []
            elif btype == "thinking":
                cb.setdefault("thinking", "")
            else:
                cb.setdefault("text", "")
            blocks_by_idx[idx] = cb
        elif ev_name == "content_block_delta":
            idx = int(data.get("index", 0))
            delta = data.get("delta") or {}
            dtype = delta.get("type")
            if idx not in blocks_by_idx:
                # 没见过 start · 按 text 兜底建块
                blocks_by_idx[idx] = {"type": "text", "text": ""}
            block = blocks_by_idx[idx]
            if dtype == "text_delta":
                block["text"] = (block.get("text") or "") + (delta.get("text") or "")
            elif dtype == "thinking_delta":
                block["thinking"] = (block.get("thinking") or "") + (delta.get("thinking") or "")
            elif dtype == "input_json_delta":
                tool_partial_json.setdefault(idx, []).append(delta.get("partial_json") or "")
        elif ev_name == "content_block_stop":
            idx = int(data.get("index", 0))
            block = blocks_by_idx.get(idx)
            if block and block.get("type") == "tool_use":
                joined = "".join(tool_partial_json.get(idx, []))
                if joined.strip():
                    try:
                        block["input"] = json.loads(joined)
                    except json.JSONDecodeError:
                        block["input"] = {}
        elif ev_name == "message_delta":
            delta = data.get("delta") or {}
            if "stop_reason" in delta:
                message["stop_reason"] = delta.get("stop_reason")
            if "stop_sequence" in delta:
                message["stop_sequence"] = delta.get("stop_sequence")
            usage = data.get("usage") or {}
            if "output_tokens" in usage:
                message["usage"]["output_tokens"] = int(usage.get("output_tokens") or 0)
            if "input_tokens" in usage:
                message["usage"]["input_tokens"] = int(usage.get("input_tokens") or 0)
        elif ev_name == "error":
            err = data.get("error") or {}
            error_msg = err.get("message") or "upstream error"

    # 按 index 顺序输出 content(丢空 text 块 · 但保留空 tool_use / thinking)
    for idx in sorted(blocks_by_idx.keys()):
        b = blocks_by_idx[idx]
        if b.get("type") == "text" and not (b.get("text") or "").strip():
            continue
        message["content"].append(b)

    if error_msg and not message["content"]:
        message["content"].append({"type": "text", "text": f"[dcc-proxy] {error_msg}"})
        message["stop_reason"] = message["stop_reason"] or "end_turn"

    if message["stop_reason"] is None:
        message["stop_reason"] = "end_turn"

    return message


def estimate_input_tokens(body: dict) -> int:
    """无内部 tokenizer 时估算 Anthropic 请求的 input token。"""
    parts: list[str] = []
    system = body.get("system", "")
    if isinstance(system, str):
        parts.append(system)
    elif isinstance(system, list):
        parts.extend(
            blk.get("text", "") for blk in system
            if isinstance(blk, dict) and blk.get("text")
        )
    for message in body.get("messages", []) or []:
        content = message.get("content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.append(json.dumps(content, ensure_ascii=False))
    if body.get("tools"):
        parts.append(json.dumps(body["tools"], ensure_ascii=False))
    return max(1, len("\n".join(parts).encode("utf-8")) // 4)


# ============================================================
# openai → anthropic 流式转换核心
# ============================================================

def stream_openai_to_anthropic(handler: "ProxyHandler", model_name: str, model: dict,
                                messages: list[dict], extra: dict,
                                body_original: dict,
                                candidates: list[dict] | None = None) -> None:
    """向京东网关 OpenAI /chat/completions 发 stream=true,边收边转 Anthropic SSE。

    URL/api_key 都从 model dict(源自 llm.ini section)拿。

    failover:candidates=[主, 候选1, 候选2...](同组同协议)。非链尾候选走软超时守卫——
    首字节超 soft_timeout / 首帧前遇可重试 429/5xx / 连接异常 → 切下一候选;链尾(或唯一)
    候选走今日原样的自 post + retry 逻辑(无候选时逐字节等价今日,零回归)。首帧一旦到达即
    锁定当前候选,绝不再切(硬约束:换模型会污染已输出内容)。
    """
    chain = candidates if candidates else [model]
    soft_timeout = CONFIG.soft_timeout
    req_id = getattr(handler, "_dcc_req_id", "")
    t_start = time.time()

    msg_id = f"msg_{uuid.uuid4().hex[:16]}"
    handler.wfile.write(emit_message_start(msg_id, model_name))
    # 图文剥离警告 · 若前置剥了图 · 首帧就打开 text block 0 + 写 warning delta
    stripped_warned = _write_stripped_images_warning(handler)
    # 以下所有 emit 状态放外层 · 首帧前重试时"回滚状态"= 保持 emitted_any=False + tool_state 空
    # 首帧后异常 → 保护关闭路径(不回滚)
    text_block_started = stripped_warned
    text_block_stopped = False
    tool_state: dict[int, dict] = {}
    next_block_idx = 1
    stop_reason = "end_turn"
    output_tokens = 0
    input_tokens = 0
    emitted_any = stripped_warned   # 是否已经吐出任何用户可见 delta(text / tool_use start / tool_input) · 决定能否重试
    # <think>...</think> 流式过滤状态(DeepSeek-V4 在 content 里塞思维链标签)
    think_state = {"in_think": False, "buf": "", "mode": None}
    reasoning_mode = _reasoning_mode()
    thinking_block_idx: int | None = None  # thinking 模式下 · 单独一个 block
    retry_after_hint: str | None = None  # 429/503 上游可能带 Retry-After · 由 _run_upstream 存
    last_error_status: int = 0            # retry 耗尽时把 upstream 错误原文透传给 CC 前端
    last_error_body: str = ""

    def _ensure_text_started():
        nonlocal text_block_started
        if not text_block_started:
            handler.wfile.write(emit_content_block_start(0))
            handler.wfile.flush()
            text_block_started = True

    def _close_text_if_open():
        nonlocal text_block_stopped
        if text_block_started and not text_block_stopped:
            handler.wfile.write(emit_content_block_stop(0))
            handler.wfile.flush()
            text_block_stopped = True

    def _ensure_thinking_started() -> int:
        """thinking 模式:首个 reasoning chunk 打开一个独立 thinking block · 返回 idx。"""
        nonlocal thinking_block_idx, next_block_idx
        if thinking_block_idx is None:
            thinking_block_idx = next_block_idx
            next_block_idx += 1
            handler.wfile.write(emit_thinking_block_start(thinking_block_idx))
            handler.wfile.flush()
        return thinking_block_idx

    def _close_thinking_if_open():
        nonlocal thinking_block_idx
        if thinking_block_idx is not None:
            handler.wfile.write(emit_content_block_stop(thinking_block_idx))
            handler.wfile.flush()
            thinking_block_idx = None

    # ── 每候选独立三要素 · 现算(chain 内各候选各有 upstream_id/base_url/api_key)
    def _build_req(cand: dict):
        cu = cand["upstream_id"]
        pl = {"model": cu, "messages": messages, "stream": True}
        pl.update(extra)
        cu_url = f"{cand['base_url']}/chat/completions"
        cu_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cand['api_key']}",
        }
        return cu, cu_url, cu_headers, pl

    primary_id = chain[0]["upstream_id"]
    switch_notice = {"text": "", "written": False}
    failover_to = ""          # 实际锁定的候选 upstream_id(切换才非空)
    candidate_attempts = 0    # 尝试过的候选数(含主模型)

    def _emit_switch_notice_once():
        """候选首个可见 delta 前注入一行切换提示 · 只写一次 · 写后置 emitted_any。"""
        nonlocal emitted_any
        if switch_notice["text"] and not switch_notice["written"]:
            _ensure_text_started()
            handler.wfile.write(emit_content_delta(0, switch_notice["text"]))
            handler.wfile.flush()
            switch_notice["written"] = True
            emitted_any = True

    def _consume_stream(line_iter) -> None:
        """SSE 读循环:逐行 OpenAI chunk → Anthropic delta · 改外层 nonlocal 状态。

        任何 emit 前置 emitted_any=True;不做 status 检查/连接管理(调用方负责)。
        切换来的候选 · 在首个可见 delta 前经 _emit_switch_notice_once 注入提示行。
        """
        nonlocal next_block_idx, stop_reason, output_tokens, input_tokens, emitted_any
        last_chunk: dict | None = None
        for line in line_iter:
            if not line:
                continue
            _tl("gw_chunk", req_id=req_id, protocol="openai", line=line)
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            last_chunk = chunk
            choices = chunk.get("choices", [])
            if not choices:
                continue
            delta = choices[0].get("delta", {})
            reasoning = delta.get("reasoning_content")
            if reasoning:
                if reasoning_mode == "thinking":
                    _emit_switch_notice_once()
                    emitted_any = True
                    idx = _ensure_thinking_started()
                    handler.wfile.write(emit_thinking_delta(idx, reasoning))
                    handler.wfile.flush()
                elif reasoning_mode == "text":
                    _emit_switch_notice_once()
                    emitted_any = True
                    _ensure_text_started()
                    handler.wfile.write(emit_content_delta(0, reasoning))
                    handler.wfile.flush()
                # hide 模式:丢
            piece = delta.get("content")
            if piece:
                if reasoning_mode == "hide":
                    # hide:剔除 <think>...</think> · 只发 visible
                    visible = _strip_think_stream(piece, think_state)
                    thinking_seg = ""
                elif reasoning_mode == "text":
                    # text:全部合并到 text_delta,不拆
                    visible = piece
                    thinking_seg = ""
                else:
                    # thinking:拆两段 · <think> 内 → thinking_delta · <think> 外 → text_delta
                    thinking_seg, visible = _split_think_stream(piece, think_state)
                if thinking_seg:
                    _emit_switch_notice_once()
                    emitted_any = True
                    idx = _ensure_thinking_started()
                    handler.wfile.write(emit_thinking_delta(idx, thinking_seg))
                    handler.wfile.flush()
                if visible:
                    # 上游从 reasoning 切到 content · 关闭 thinking block
                    if thinking_block_idx is not None:
                        _close_thinking_if_open()
                    _emit_switch_notice_once()
                    emitted_any = True
                    _ensure_text_started()
                    handler.wfile.write(emit_content_delta(0, visible))
                    handler.wfile.flush()
            for tc in delta.get("tool_calls") or []:
                tc_idx = tc.get("index", 0)
                st = tool_state.get(tc_idx)
                if st is None:
                    _emit_switch_notice_once()
                    _close_thinking_if_open()
                    _close_text_if_open()
                    tool_id = tc.get("id") or f"toolu_{uuid.uuid4().hex[:16]}"
                    fn = tc.get("function") or {}
                    name = fn.get("name") or ""
                    st = {
                        "id": tool_id,
                        "name": name,
                        "args_buf": "",
                        "block_idx": next_block_idx,
                        "started": False,
                    }
                    tool_state[tc_idx] = st
                    next_block_idx += 1
                fn = tc.get("function") or {}
                if fn.get("name") and not st["started"]:
                    st["name"] = fn["name"]
                if st["name"] and not st["started"]:
                    emitted_any = True
                    handler.wfile.write(emit_tool_use_start(st["block_idx"], st["id"], st["name"]))
                    handler.wfile.flush()
                    st["started"] = True
                args_part = fn.get("arguments")
                if args_part:
                    st["args_buf"] += args_part
                    if st["started"]:
                        emitted_any = True
                        handler.wfile.write(emit_tool_input_delta(st["block_idx"], args_part))
                        handler.wfile.flush()
            fr = choices[0].get("finish_reason")
            if fr in ("stop", "length", "tool_calls"):
                if fr == "length":
                    stop_reason = "max_tokens"
                elif fr == "tool_calls":
                    stop_reason = "tool_use"
                else:
                    stop_reason = "end_turn"
        if last_chunk and last_chunk.get("usage"):
            output_tokens = last_chunk["usage"].get("completion_tokens", 0)
            input_tokens = last_chunk["usage"].get("prompt_tokens", 0)
            _log(f"[openai] usage: prompt_tokens={input_tokens} completion_tokens={output_tokens}")

    def _run_tail_upstream(upstream_id, url, headers, payload) -> bool:
        """链尾(或唯一)候选:今日原样自 post + 状态检查 + 读循环。

        返回 True=读完/已 emit 错误 · False=首帧前可重试错误(交外层 retry) · 抛异常=流断。
        无候选时 chain=[主],走此路 → 逐字节等价改造前,零回归。
        """
        nonlocal emitted_any, retry_after_hint, last_error_status, last_error_body
        retry_after_hint = None
        with requests.post(
            url, json=payload, headers=headers, stream=True,
            timeout=(STREAM_CONNECT_TIMEOUT, STREAM_IDLE_TIMEOUT),
        ) as r:
            _tl("gw_status", req_id=req_id, protocol="openai", status=r.status_code)
            if r.status_code >= 400:
                body_preview = r.text[:1500]
                _log(f"openai upstream ERROR status={r.status_code} body={body_preview}")
                last_error_status = r.status_code
                last_error_body = body_preview
                if not emitted_any and is_retryable_status(r.status_code):
                    retry_after_hint = r.headers.get("Retry-After")
                    return False
                dump_path = _dump_request(upstream_id, body_original, payload, body_preview)
                if dump_path:
                    _log(f"[ERROR] 请求 body 已落盘: {dump_path}")
                emitted_any = True
                _ensure_text_started()
                handler.wfile.write(emit_content_delta(0, f"[dcc-proxy] openai {format_upstream_error(r.status_code, body_preview)}"))
                handler.wfile.flush()
                return True  # non-retryable 4xx / 已 emit · 不重试
            _emit_switch_notice_once()
            r.encoding = "utf-8"
            _consume_stream(r.iter_lines(decode_unicode=True, chunk_size=1))
        return True

    def _run_tail_with_retry(upstream_id, url, headers, payload) -> None:
        """链尾候选的重试外层 · 完整保留改造前 openai 的 while-True 重试/保护关闭语义。"""
        nonlocal emitted_any
        attempts = 0
        retry_attempts = 0
        while True:
            attempts += 1
            try:
                ok = _run_tail_upstream(upstream_id, url, headers, payload)
                if ok:
                    return
                # _run_tail_upstream 返 False:上游 retryable status(429/5xx)· 首帧前
                if retry_attempts >= MAX_RETRIES:
                    _log(f"[ERROR] openai retryable status 超过 {MAX_RETRIES} 次 · 放弃 · last={last_error_status} body={last_error_body[:500]}")
                    emitted_any = True
                    _ensure_text_started()
                    msg = f"[dcc-proxy] openai {format_upstream_error(last_error_status, last_error_body, retries=retry_attempts)}"
                    handler.wfile.write(emit_content_delta(0, msg))
                    handler.wfile.flush()
                    return
                retry_attempts += 1
                delay = compute_backoff(retry_attempts, retry_after_hint)
                _log(f"[WARN] openai upstream retryable · attempt={retry_attempts}/{MAX_RETRIES} · sleep={delay:.1f}s · retry_after={retry_after_hint}")
                time.sleep(delay)
                continue
            except _STREAM_RETRY_EXC as e:
                if not emitted_any and attempts <= STREAM_MAX_RETRIES:
                    _log(f"[WARN] openai stream break before first byte · {type(e).__name__}: {e} · retry {attempts}/{STREAM_MAX_RETRIES}")
                    continue
                # 首帧已 emit → 保护关闭 · 让 CC 端能干净结尾
                _log(f"[ERROR] openai stream break after emit(attempts={attempts}) · {type(e).__name__}: {e}")
                try:
                    emitted_any = True
                    _ensure_text_started()
                    handler.wfile.write(emit_content_delta(0, f"\n\n[dcc-proxy] upstream stream broke: {type(e).__name__}"))
                    handler.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            except requests.RequestException as e:
                _log(f"[ERROR] openai request failed: {e}")
                try:
                    emitted_any = True
                    _ensure_text_started()
                    handler.wfile.write(emit_content_delta(0, f"[dcc-proxy] upstream error: {e}"))
                    handler.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return

    # ── failover 主循环:逐候选尝试 · 非链尾走软超时守卫 · 链尾走今日原样 retry ──
    # 首帧一旦到达(emitted_any)即锁定当前候选,循环终止,绝不再切(硬约束)。
    for i, cand in enumerate(chain):
        candidate_attempts += 1
        cu, cu_url, cu_headers, cu_payload = _build_req(cand)
        is_tail = (i == len(chain) - 1)
        if not is_tail and CONFIG.max_candidates > 1:
            # 非链尾:守卫连接 · 首字节超时/首帧前 429/5xx/连断 → 切下一候选
            _log(f"→ openai {cu} · guarded(soft={soft_timeout:.0f}s) · cand={i+1}/{len(chain)} · url={cu_url} · msgs={len(messages)}")
            _tl("gw_req", req_id=req_id, protocol="openai", url=cu_url, upstream_id=cu, payload=cu_payload, candidate=i + 1)
            try:
                conn = _guarded_connect(
                    cu_url, cu_payload, cu_headers, soft_timeout=soft_timeout,
                    decode_unicode=True, req_id=req_id, protocol="openai",
                )
            except _SoftTimeout:
                _log(f"[WARN] openai {cu} 首字节 {soft_timeout:.0f}s 未响应 · 切下一候选")
                _tl("failover", req_id=req_id, protocol="openai", reason="soft_timeout", from_id=cu, soft_timeout=soft_timeout)
                continue
            except _Upstream429 as e:
                _log(f"[WARN] openai {cu} 首帧前 upstream {e.status} · switch_on_429={CONFIG.switch_on_429} · 切下一候选")
                _tl("failover", req_id=req_id, protocol="openai", reason=f"upstream_{e.status}", from_id=cu)
                continue
            except _STREAM_RETRY_EXC as e:
                _log(f"[WARN] openai {cu} 首帧前连接异常 · {type(e).__name__}: {e} · 切下一候选")
                _tl("failover", req_id=req_id, protocol="openai", reason=type(e).__name__, from_id=cu)
                continue
            except requests.RequestException as e:
                _log(f"[WARN] openai {cu} 连接失败 · {type(e).__name__}: {e} · 切下一候选")
                _tl("failover", req_id=req_id, protocol="openai", reason=type(e).__name__, from_id=cu)
                continue
            # 守卫成功 · 首字节已到 · 锁定本候选 · 若非主模型则备好切换提示
            if i > 0:
                failover_to = cu
                switch_notice["text"] = (
                    f"[dcc] 主模型 {primary_id} 首字节 {soft_timeout:.0f}s 未响应 · 已切至 {cu}\n\n"
                )
                _log(f"[INFO] openai failover {primary_id} → {cu}(cand {i+1}/{len(chain)})")
                _tl("failover_locked", req_id=req_id, protocol="openai", from_id=primary_id, to_id=cu, candidate=i + 1)
            with conn.response:
                try:
                    _consume_stream(conn.iter_all())
                except _STREAM_RETRY_EXC as e:
                    # 首帧后流断 → 保护关闭(不切;硬约束)· 首帧前(空响应)也在此收尾
                    _log(f"[ERROR] openai stream break(guarded {cu}) · {type(e).__name__}: {e}")
                    try:
                        emitted_any = True
                        _ensure_text_started()
                        handler.wfile.write(emit_content_delta(0, f"\n\n[dcc-proxy] upstream stream broke: {type(e).__name__}"))
                        handler.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
            break
        else:
            # 链尾(或唯一 · 或 max_candidates<=1):今日原样自 post + retry(零回归)
            if i > 0:
                failover_to = cu
                switch_notice["text"] = (
                    f"[dcc] 主模型 {primary_id} 首字节 {soft_timeout:.0f}s 未响应 · 已切至 {cu}\n\n"
                )
                _log(f"[INFO] openai failover {primary_id} → {cu}(链尾候选 {i+1}/{len(chain)})")
                _tl("failover_locked", req_id=req_id, protocol="openai", from_id=primary_id, to_id=cu, candidate=i + 1)
            _log(f"→ openai {cu} · url={cu_url} · msgs={len(messages)} · max_tok={extra.get('max_tokens','-')} · tools={len(extra.get('tools') or [])}")
            _tl("gw_req", req_id=req_id, protocol="openai", url=cu_url, upstream_id=cu, payload=cu_payload, candidate=i + 1)
            _run_tail_with_retry(cu, cu_url, cu_headers, cu_payload)
            break

    # stream 结束 · flush think 过滤器 buf 里剩余
    if think_state["buf"] and think_state.get("mode") == "tag" and not think_state["in_think"]:
        _ensure_text_started()
        handler.wfile.write(emit_content_delta(0, think_state["buf"]))
        handler.wfile.flush()

    # 关掉 thinking 块(如未关)· 关掉 text 块(如未关)· 关掉所有 tool_use 块
    _close_thinking_if_open()
    _close_text_if_open()
    for st in tool_state.values():
        if st["started"]:
            handler.wfile.write(emit_content_block_stop(st["block_idx"]))
    handler.wfile.write(emit_message_delta(stop_reason, {"input_tokens": input_tokens, "output_tokens": output_tokens}))
    handler.wfile.write(emit_message_stop())
    handler.wfile.flush()
    _tl(
        "done", req_id=req_id, protocol="openai",
        stop_reason=stop_reason, input_tokens=input_tokens, output_tokens=output_tokens,
        tools_emitted=len(tool_state), emitted_any=emitted_any, dt=round(time.time() - t_start, 3),
        failover_from=(primary_id if failover_to else ""), failover_to=failover_to,
        candidate_attempts=candidate_attempts,
    )


def stream_responses_to_anthropic(handler: "ProxyHandler", model_name: str, model: dict,
                                  messages: list[dict], extra: dict, body_original: dict,
                                  candidates: list[dict] | None = None) -> None:
    """Responses /v1/responses 走 instructions/input 结构并转换为 Anthropic SSE。

    failover:candidates=[主, 候选1, 候选2...](同组同协议)。非链尾候选走软超时守卫——
    首字节超 soft_timeout / 首帧前遇可重试 429/5xx / 连接异常 → 切下一候选;链尾(或唯一)
    候选走原样的自 post + retry 逻辑(无候选时 chain=[主],逐字节等价改造前,零回归)。首帧
    一旦到达即锁定当前候选,绝不再切(硬约束:换模型会污染已输出内容)。
    """
    chain = candidates if candidates else [model]
    soft_timeout = CONFIG.soft_timeout
    system_text = "\n\n".join(
        m.get("content", "") for m in messages if m.get("role") == "system" and isinstance(m.get("content"), str)
    )
    input_messages = [m for m in messages if m.get("role") != "system"]
    req_id = getattr(handler, "_dcc_req_id", "")

    msg_id = f"msg_{uuid.uuid4().hex[:16]}"
    estimated_input_tokens = estimate_input_tokens(body_original)
    handler.wfile.write(emit_message_start(msg_id, model_name, estimated_input_tokens))
    # 图文剥离警告 · 若剥了图 · 此 helper 已开 block 0 并写 warning · 跳过下面自己的 block_start(0)
    if not _write_stripped_images_warning(handler):
        handler.wfile.write(emit_content_block_start(0))
    handler.wfile.flush()
    output_tokens = 0
    input_tokens = 0
    stop_reason = "end_turn"
    text_block_stopped = False
    tool_states: dict[str, dict] = {}
    next_block_idx = 1
    emitted_any = False
    retry_after_hint: str | None = None
    last_error_status: int = 0
    last_error_body: str = ""
    t0 = time.time()

    primary_id = chain[0]["upstream_id"]
    switch_notice = {"text": "", "written": False}
    failover_to = ""          # 实际锁定的候选 upstream_id(切换才非空)
    candidate_attempts = 0    # 尝试过的候选数(含主模型)

    # ── 每候选独立三要素 · 现算(chain 内各候选各有 upstream_id/base_url/api_key)
    def _build_responses_req(cand: dict):
        cu = cand["upstream_id"]
        pl = {
            "model": cu,
            "instructions": system_text or "You are a helpful assistant.",
            "input": _openai_messages_to_responses_input(input_messages),
            "stream": True,
        }
        if extra.get("tools"):
            pl["tools"] = _openai_tools_to_responses(extra["tools"])
        if extra.get("tool_choice"):
            pl["tool_choice"] = extra["tool_choice"]
        if extra.get("max_tokens"):
            pl["max_output_tokens"] = extra["max_tokens"]
        if extra.get("temperature") is not None:
            pl["temperature"] = extra["temperature"]
        cu_url = f"{cand['base_url']}/responses"
        cu_headers = {"Content-Type": "application/json", "Authorization": f"Bearer {cand['api_key']}"}
        return cu, cu_url, cu_headers, pl

    def close_text_block() -> None:
        nonlocal text_block_stopped
        if not text_block_stopped:
            handler.wfile.write(emit_content_block_stop(0))
            text_block_stopped = True

    def _emit_switch_notice_once():
        """候选首个可见 delta 前注入一行切换提示 · 只写一次 · 写后置 emitted_any。"""
        nonlocal emitted_any
        if switch_notice["text"] and not switch_notice["written"]:
            handler.wfile.write(emit_content_delta(0, switch_notice["text"]))
            handler.wfile.flush()
            switch_notice["written"] = True
            emitted_any = True

    def _consume_responses_stream(line_iter) -> None:
        """SSE 读循环:逐行 responses event → Anthropic delta · 改外层 nonlocal 状态。

        任何 emit 前置 emitted_any=True;不做 status 检查/连接管理(调用方负责)。
        切换来的候选 · 在首个可见 delta 前经 _emit_switch_notice_once 注入提示行。
        """
        nonlocal output_tokens, input_tokens, stop_reason, next_block_idx, emitted_any
        for raw_line in line_iter:
            if not raw_line or not raw_line.strip():
                continue
            _tl("gw_chunk", req_id=req_id, protocol="responses", line=raw_line)
            line = raw_line.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if line == "[DONE]":
                break
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_type = event.get("type")
            if event_type == "response.output_text.delta" and event.get("delta"):
                _emit_switch_notice_once()
                emitted_any = True
                handler.wfile.write(emit_content_delta(0, event["delta"]))
                handler.wfile.flush()
            elif event_type == "response.output_item.added":
                item = event.get("item") or {}
                if item.get("type") == "function_call":
                    _emit_switch_notice_once()
                    close_text_block()
                    item_id = item.get("id", "")
                    call_id = item.get("call_id") or item_id
                    block_idx = next_block_idx
                    next_block_idx += 1
                    tool_states[item_id] = {
                        "block_idx": block_idx,
                        "call_id": call_id,
                        "name": item.get("name", ""),
                    }
                    emitted_any = True
                    handler.wfile.write(emit_tool_use_start(block_idx, call_id, item.get("name", "")))
                    handler.wfile.flush()
            elif event_type == "response.function_call_arguments.delta":
                item_id = event.get("item_id", "")
                state = tool_states.get(item_id)
                if state and event.get("delta"):
                    emitted_any = True
                    handler.wfile.write(emit_tool_input_delta(state["block_idx"], event["delta"]))
                    handler.wfile.flush()
            elif event_type == "response.completed":
                usage = event.get("response", {}).get("usage") or {}
                output_tokens = usage.get("output_tokens", 0)
                input_tokens = usage.get("input_tokens", 0)
                if tool_states:
                    stop_reason = "tool_use"
                _log(f"[responses] usage: input_tokens={input_tokens} output_tokens={output_tokens} tools={len(tool_states)}")

    def _run_tail_upstream(upstream_id, url, headers, payload) -> bool:
        """链尾(或唯一)候选:原样自 post + 状态检查 + 读循环。

        返回 True=读完/已 emit 错误 · False=首帧前可重试错误(交外层 retry) · 抛异常=流断。
        无候选时 chain=[主],走此路 → 逐字节等价改造前,零回归。
        """
        nonlocal emitted_any, retry_after_hint, last_error_status, last_error_body
        retry_after_hint = None
        with requests.post(
            url, json=payload, headers=headers, stream=True,
            timeout=(STREAM_CONNECT_TIMEOUT, STREAM_IDLE_TIMEOUT),
        ) as r:
            _log(f"responses upstream connected · dt={time.time()-t0:.1f}s · status={r.status_code}")
            _tl("gw_status", req_id=req_id, protocol="responses", status=r.status_code, dt=round(time.time() - t0, 3))
            if r.status_code >= 400:
                preview = r.text[:1500]
                _log(f"responses upstream ERROR status={r.status_code} body={preview}")
                last_error_status = r.status_code
                last_error_body = preview
                if not emitted_any and is_retryable_status(r.status_code):
                    retry_after_hint = r.headers.get("Retry-After")
                    return False
                _dump_request(upstream_id, body_original, payload, preview)
                emitted_any = True
                handler.wfile.write(emit_content_delta(0, f"[dcc-proxy] responses {format_upstream_error(r.status_code, preview)}"))
                handler.wfile.flush()
                return True
            _emit_switch_notice_once()
            r.encoding = "utf-8"
            _consume_responses_stream(r.iter_lines(decode_unicode=True, chunk_size=1))
        return True

    def _run_tail_with_retry(upstream_id, url, headers, payload) -> None:
        """链尾候选的重试外层 · 完整保留改造前 responses 的 while-True 重试/保护关闭语义。"""
        nonlocal emitted_any
        attempts = 0
        retry_attempts = 0
        while True:
            attempts += 1
            try:
                ok = _run_tail_upstream(upstream_id, url, headers, payload)
                if ok:
                    return
                if retry_attempts >= MAX_RETRIES:
                    _log(f"[ERROR] responses retryable status 超过 {MAX_RETRIES} 次 · 放弃 · last={last_error_status} body={last_error_body[:500]}")
                    emitted_any = True
                    msg = f"[dcc-proxy] responses {format_upstream_error(last_error_status, last_error_body, retries=retry_attempts)}"
                    handler.wfile.write(emit_content_delta(0, msg))
                    handler.wfile.flush()
                    return
                retry_attempts += 1
                delay = compute_backoff(retry_attempts, retry_after_hint)
                _log(f"[WARN] responses upstream retryable · attempt={retry_attempts}/{MAX_RETRIES} · sleep={delay:.1f}s · retry_after={retry_after_hint}")
                time.sleep(delay)
                continue
            except _STREAM_RETRY_EXC as e:
                if not emitted_any and attempts <= STREAM_MAX_RETRIES:
                    _log(f"[WARN] responses stream break before first byte · {type(e).__name__}: {e} · retry {attempts}/{STREAM_MAX_RETRIES}")
                    continue
                _log(f"[ERROR] responses stream break after emit(attempts={attempts}) · {type(e).__name__}: {e}")
                try:
                    emitted_any = True
                    handler.wfile.write(emit_content_delta(0, f"\n\n[dcc-proxy] upstream stream broke: {type(e).__name__}"))
                    handler.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            except requests.RequestException as e:
                _log(f"[ERROR] responses request failed: {e}")
                try:
                    emitted_any = True
                    handler.wfile.write(emit_content_delta(0, f"[dcc-proxy] upstream error: {e}"))
                    handler.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return

    try:
        # ── failover 主循环:逐候选尝试 · 非链尾走软超时守卫 · 链尾走原样 retry ──
        # 首帧一旦到达(emitted_any)即锁定当前候选,循环终止,绝不再切(硬约束)。
        for i, cand in enumerate(chain):
            candidate_attempts += 1
            cu, cu_url, cu_headers, cu_payload = _build_responses_req(cand)
            is_tail = (i == len(chain) - 1)
            if not is_tail and CONFIG.max_candidates > 1:
                # 非链尾:守卫连接 · 首字节超时/首帧前 429/5xx/连断 → 切下一候选
                _log(f"→ responses {cu} · guarded(soft={soft_timeout:.0f}s) · cand={i+1}/{len(chain)} · url={cu_url} · input={len(input_messages)}")
                _tl("gw_req", req_id=req_id, protocol="responses", url=cu_url, upstream_id=cu, payload=cu_payload, candidate=i + 1)
                try:
                    conn = _guarded_connect(
                        cu_url, cu_payload, cu_headers, soft_timeout=soft_timeout,
                        decode_unicode=True, req_id=req_id, protocol="responses",
                    )
                except _SoftTimeout:
                    _log(f"[WARN] responses {cu} 首字节 {soft_timeout:.0f}s 未响应 · 切下一候选")
                    _tl("failover", req_id=req_id, protocol="responses", reason="soft_timeout", from_id=cu, soft_timeout=soft_timeout)
                    continue
                except _Upstream429 as e:
                    _log(f"[WARN] responses {cu} 首帧前 upstream {e.status} · switch_on_429={CONFIG.switch_on_429} · 切下一候选")
                    _tl("failover", req_id=req_id, protocol="responses", reason=f"upstream_{e.status}", from_id=cu)
                    continue
                except _STREAM_RETRY_EXC as e:
                    _log(f"[WARN] responses {cu} 首帧前连接异常 · {type(e).__name__}: {e} · 切下一候选")
                    _tl("failover", req_id=req_id, protocol="responses", reason=type(e).__name__, from_id=cu)
                    continue
                except requests.RequestException as e:
                    _log(f"[WARN] responses {cu} 连接失败 · {type(e).__name__}: {e} · 切下一候选")
                    _tl("failover", req_id=req_id, protocol="responses", reason=type(e).__name__, from_id=cu)
                    continue
                # 守卫成功 · 首字节已到 · 锁定本候选 · 若非主模型则备好切换提示
                if i > 0:
                    failover_to = cu
                    switch_notice["text"] = (
                        f"[dcc] 主模型 {primary_id} 首字节 {soft_timeout:.0f}s 未响应 · 已切至 {cu}\n\n"
                    )
                    _log(f"[INFO] responses failover {primary_id} → {cu}(cand {i+1}/{len(chain)})")
                    _tl("failover_locked", req_id=req_id, protocol="responses", from_id=primary_id, to_id=cu, candidate=i + 1)
                with conn.response:
                    try:
                        _consume_responses_stream(conn.iter_all())
                    except _STREAM_RETRY_EXC as e:
                        # 首帧后流断 → 保护关闭(不切;硬约束)· 首帧前(空响应)也在此收尾
                        _log(f"[ERROR] responses stream break(guarded {cu}) · {type(e).__name__}: {e}")
                        try:
                            emitted_any = True
                            handler.wfile.write(emit_content_delta(0, f"\n\n[dcc-proxy] upstream stream broke: {type(e).__name__}"))
                            handler.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                break
            else:
                # 链尾(或唯一 · 或 max_candidates<=1):原样自 post + retry(零回归)
                if i > 0:
                    failover_to = cu
                    switch_notice["text"] = (
                        f"[dcc] 主模型 {primary_id} 首字节 {soft_timeout:.0f}s 未响应 · 已切至 {cu}\n\n"
                    )
                    _log(f"[INFO] responses failover {primary_id} → {cu}(链尾候选 {i+1}/{len(chain)})")
                    _tl("failover_locked", req_id=req_id, protocol="responses", from_id=primary_id, to_id=cu, candidate=i + 1)
                _log(f"→ responses {cu} · url={cu_url} · input={len(input_messages)}")
                _tl("gw_req", req_id=req_id, protocol="responses", url=cu_url, upstream_id=cu, payload=cu_payload, candidate=i + 1)
                _run_tail_with_retry(cu, cu_url, cu_headers, cu_payload)
                break
    finally:
        if not text_block_stopped:
            close_text_block()
        for state in tool_states.values():
            handler.wfile.write(emit_content_block_stop(state["block_idx"]))
        handler.wfile.write(emit_message_delta(stop_reason, {"input_tokens": input_tokens, "output_tokens": output_tokens}))
        handler.wfile.write(emit_message_stop())
        handler.wfile.flush()
        _tl(
            "done", req_id=req_id, protocol="responses",
            stop_reason=stop_reason, input_tokens=input_tokens, output_tokens=output_tokens,
            tools_emitted=len(tool_states), emitted_any=emitted_any, dt=round(time.time() - t0, 3),
            failover_to=failover_to, candidate_attempts=candidate_attempts,
        )


# ============================================================
# HTTP handler
# ============================================================

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    server_version = "dcc-proxy/1"

    def log_message(self, format: str, *args) -> None:  # noqa
        _log(f"{self.client_address[0]}:{self.client_address[1]} {format % args}")

    def _path_only(self) -> str:
        p = self.path
        i = p.find("?")
        return p[:i] if i >= 0 else p

    def do_HEAD(self) -> None:  # noqa
        # claude CLI 启动预检打 HEAD /api/hello 判断是不是 Anthropic 端点
        path = self._path_only()
        if path in ("/health", "/health/liveliness", "/api/hello", "/v1/messages"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self) -> None:  # noqa
        path = self._path_only()
        if path in ("/health", "/health/liveliness", "/api/hello"):
            self._send_json(200, {"status": "ok", "version": DCC_VERSION})
            return
        if path == "/v1/models":
            models = [
                {"id": m["name"], "protocol": m["protocol"], "upstream": m["upstream_id"], "base_url": m["base_url"], "section": m.get("section")}
                for m in CONFIG.models_by_name.values()
            ]
            self._send_json(200, {"models": models, "version": DCC_VERSION})
            return
        self._send_json(404, {"error": f"unknown path {self.path}"})

    def do_POST(self) -> None:  # noqa
        path = self._path_only()
        if path == "/v1/messages/count_tokens":
            self._handle_count_tokens()
            return
        if path not in ("/v1/messages",):
            self._send_json(404, {"error": f"unknown path {self.path}"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": f"bad json: {e}"})
            return

        model_name = body.get("model", "")
        m = CONFIG.resolve(model_name)
        req_id = uuid.uuid4().hex[:12]
        self._dcc_req_id = req_id  # stream_xxx 通过 handler 拿
        _tl(
            "cc_in",
            req_id=req_id,
            client=f"{self.client_address[0]}:{self.client_address[1]}",
            model=model_name,
            resolved=(m or {}).get("name"),
            protocol=(m or {}).get("protocol"),
            upstream_id=(m or {}).get("upstream_id"),
            stream=bool(body.get("stream", False)),
            msgs=len(body.get("messages") or []),
            tools=len(body.get("tools") or []),
            max_tokens=body.get("max_tokens"),
            body=body,
        )
        if not m:
            _tl("done", req_id=req_id, phase="resolve", status=404, error=f"model not configured: {model_name}")
            self._send_json(404, {"error": f"model not configured: {model_name}"})
            return

        # 图文能力守卫:m.supports_images=false 时剥掉 body.messages 里所有 image · 记录数量
        # · 触发场景 · 上一轮 CC 会话带截图(png/jpg base64)· 本轮换到 GLM/DeepSeek 等纯文本模型
        # · 不剥图 → 上游 400「模型服务调用失败」(见 dumps/req_*_GLM-*.json msg[N] image_url)
        # · dcc 明文提示 · 首个 assistant text_delta 前注入一段 warning
        stripped_imgs = _strip_images_if_unsupported(body, m)
        self._dcc_stripped_imgs = stripped_imgs  # stream_*_to_anthropic 通过 handler 拿 · 首帧插 warning
        self._dcc_slot = m.get("slot", "?")
        self._dcc_upstream = m.get("upstream_id", "?")
        if stripped_imgs > 0:
            _log(f"[WARN] slot={m.get('slot')} upstream={m.get('upstream_id')} 不支持图文 · 已剥离 {stripped_imgs} 张图片")
            _tl("image_stripped", req_id=req_id, slot=m.get("slot"), upstream_id=m.get("upstream_id"), count=stripped_imgs)

        # 抓一次真实 request body(debug tool_use) · 只在 tools 非空时打
        if body.get("tools"):
            tools = body["tools"]
            _log(f"DEBUG tools count={len(tools)} first_3_names={[t.get('name','?') for t in tools[:3]]}")

        # 有 tool_result 的 assistant 上下文 · 也 dump 一份(帮定位 model echo bug)
        # 环境变量 DCC_DEBUG_DUMP=1 时强制每请求都 dump
        has_tool_result = any(
            isinstance(msg.get("content"), list)
            and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in msg["content"])
            for msg in body.get("messages", [])
        )
        if has_tool_result or os.environ.get("DCC_DEBUG_DUMP") == "1":
            _dump_request(m["upstream_id"], body, {}, "(debug pre-request dump)")

        messages, extra = anthropic_to_openai_messages(body)
        stream = bool(body.get("stream", False))
        # openai 路 failover 候选链(同组同协议)· 主模型首字节软超时/首帧前 429 时切下一个
        # · responses/anthropic 暂不切(无同协议候选)· 走各自单模型逻辑
        openai_chain = CONFIG.resolve_candidates(model_name)

        if stream:
            # 流式:SSE 直出
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            # 关掉 keep-alive · SSE 场景下 handler 结束就该关 socket · 保 alive 会让客户端等 20s+
            self.send_header("Connection", "close")
            # 观测头:客户端凭 req_id 反查 traffic_log · slot/protocol/upstream 让调试者一眼定位路由
            # 参 CCR features/mergeFallbackResponseHeaders · 我们只做基础三头,不做 fallback trace(P1 已决定不做 fallback)
            self.send_header("x-dcc-req-id", req_id)
            self.send_header("x-dcc-slot", str(m.get("slot", "")))
            self.send_header("x-dcc-protocol", m.get("protocol", ""))
            self.send_header("x-dcc-upstream", m.get("upstream_id", ""))
            if stripped_imgs > 0:
                self.send_header("x-dcc-warn-images-stripped", str(stripped_imgs))
            self.end_headers()

            try:
                if m["protocol"] == "responses":
                    stream_responses_to_anthropic(self, model_name, m, messages, extra, body, openai_chain)
                elif m["protocol"] == "anthropic":
                    stream_anthropic_to_anthropic(self, model_name, m, body)
                else:
                    stream_openai_to_anthropic(self, model_name, m, messages, extra, body, openai_chain)
            except (BrokenPipeError, ConnectionResetError):
                _log("client disconnected mid-stream")
            except Exception as e:
                _log(f"ERROR: {e}\n{traceback.format_exc()}")
                try:
                    self.wfile.write(emit_error(str(e)))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            return

        # 非流式:复用 stream_*_to_anthropic 生产 SSE · 聚合成 anthropic Message JSON 一次性回
        # 触发场景:CC 在流式重试链路后端主动降级为 stream=false 探测(2026-08-19 slot 7 429 后 CC 报错锚点)
        real_wfile = self.wfile
        buf = _BufWFile()
        self.wfile = buf  # 三个 stream_* 只碰 handler.wfile · 无侵入替换
        try:
            if m["protocol"] == "responses":
                stream_responses_to_anthropic(self, model_name, m, messages, extra, body, openai_chain)
            elif m["protocol"] == "anthropic":
                stream_anthropic_to_anthropic(self, model_name, m, body)
            else:
                stream_openai_to_anthropic(self, model_name, m, messages, extra, body, openai_chain)
        finally:
            self.wfile = real_wfile

        fallback_msg_id = f"msg_{req_id}"
        payload = collect_sse_to_message(buf.getvalue(), fallback_model=model_name, fallback_msg_id=fallback_msg_id)
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.send_header("x-dcc-req-id", req_id)
            self.send_header("x-dcc-slot", str(m.get("slot", "")))
            self.send_header("x-dcc-protocol", m.get("protocol", ""))
            self.send_header("x-dcc-upstream", m.get("upstream_id", ""))
            if stripped_imgs > 0:
                self.send_header("x-dcc-warn-images-stripped", str(stripped_imgs))
            self.end_headers()
            self.wfile.write(raw)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            _log("client disconnected before non-stream response written")

    def _send_json(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle_count_tokens(self) -> None:
        """POST /v1/messages/count_tokens — 为 Claude Code 提供 token 计数。

        策略:
        - 无 tokenizer · 本地估算(每 4 字节 ≈ 1 token, 与 dge 一致)
        - 上游可用时(如 openai /v1/tokenizers)走上游 · 否则本地兜底
        - 返回非流式 JSON: {"input_tokens": N}
        """
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": f"bad json: {e}"})
            return

        model_name = body.get("model", "")
        m = CONFIG.resolve(model_name)
        if not m:
            self._send_json(404, {"error": f"model not configured: {model_name}"})
            return

        # 本地估算: 把 messages + system + tools 串成 UTF-8 字符串, len/4
        try:
            estimated = estimate_input_tokens(body)
            utf8_bytes = estimated * 4
            _log(f"[count_tokens] model={model_name} · 本地估算={estimated} · utf8_bytes≈{utf8_bytes}")
            self._send_json(200, {"input_tokens": estimated})
        except (KeyError, TypeError, ValueError) as e:
            _log(f"[WARN] count_tokens 解析失败: {e}")
            self._send_json(200, {"input_tokens": 0})


def stream_anthropic_to_anthropic(handler: "ProxyHandler", model_name: str, model: dict, body: dict) -> None:
    """京东网关支持原生 Anthropic 协议 · 直接转发 + 修改 model + 注入 auth。

    llm.ini section.base_url 通常是 /anthropic · 拼 /v1/messages。

    合并 messages 里 role=system 到顶层 system 字段:Anthropic API 不认 messages 内 role=system
    (京东 Claude-Opus-4.7 网关严格校验:role 'system' is not supported on this model)。
    """
    upstream_id = model["upstream_id"]
    payload = dict(body)
    payload["model"] = upstream_id
    payload["stream"] = True

    # 抽 messages 内 role=system → 顶层 system 字段
    sys_from_msgs: list[str] = []
    new_messages: list = []
    for m in payload.get("messages", []) or []:
        if m.get("role") == "system":
            c = m.get("content", "")
            if isinstance(c, str) and c.strip():
                sys_from_msgs.append(c)
            elif isinstance(c, list):
                for blk in c:
                    if isinstance(blk, dict) and blk.get("type") == "text" and blk.get("text"):
                        sys_from_msgs.append(blk["text"])
            continue
        new_messages.append(m)
    if sys_from_msgs:
        top_sys = payload.get("system")
        if isinstance(top_sys, str):
            merged = top_sys + "\n\n" + "\n\n".join(sys_from_msgs)
        elif isinstance(top_sys, list):
            # list 形态 · 追加 text 块
            merged = list(top_sys) + [{"type": "text", "text": "\n\n".join(sys_from_msgs)}]
        else:
            merged = "\n\n".join(sys_from_msgs)
        payload["system"] = merged
        payload["messages"] = new_messages
        _log(f"anthropic · 抽出 {len(sys_from_msgs)} 条 role=system 合并到顶层 system 字段")

    url = f"{model['base_url']}/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {model['api_key']}",
    }
    t0 = time.time()
    req_id = getattr(handler, "_dcc_req_id", "")
    _log(f"→ anthropic {upstream_id} · url={url} · body_keys={list(payload.keys())} · tools={len(payload.get('tools') or [])}")
    _tl("gw_req", req_id=req_id, protocol="anthropic", url=url, upstream_id=upstream_id, payload=payload)

    emitted_any = False
    retry_after_hint: str | None = None
    last_error_status: int = 0
    last_error_body: str = ""

    def _run_upstream() -> bool:
        """返回 True=正常读完 · False=可重试 status(首帧前)· 抛异常=流断。"""
        nonlocal emitted_any, retry_after_hint, last_error_status, last_error_body
        retry_after_hint = None
        with requests.post(
            url, json=payload, headers=headers, stream=True,
            timeout=(STREAM_CONNECT_TIMEOUT, STREAM_IDLE_TIMEOUT),
        ) as r:
            _tl("gw_status", req_id=req_id, protocol="anthropic", status=r.status_code, dt=round(time.time() - t0, 3))
            if r.status_code >= 400:
                body_preview = r.text[:1000]
                _log(f"[ERROR] anthropic upstream {r.status_code}: {body_preview}")
                last_error_status = r.status_code
                last_error_body = body_preview
                if not emitted_any and is_retryable_status(r.status_code):
                    retry_after_hint = r.headers.get("Retry-After")
                    return False
                dump_path = _dump_request(upstream_id, body, payload, body_preview)
                if dump_path:
                    _log(f"[ERROR] 请求 body 已落盘: {dump_path}")
                emitted_any = True
                handler.wfile.write(emit_error(f"[dcc-proxy] anthropic {format_upstream_error(r.status_code, body_preview)}"))
                handler.wfile.flush()
                return True

            # Anthropic ↔ Anthropic: 透传但恢复 SSE 空行分隔;message_start.message.model 覆写为
            # cc 端请求的 model 名(避免"我明明问 opus-4-7 · 上游回 Claude-Opus-4.7-joybuilder"的困惑)
            # 参 CCR features/anthropic-response-model.ts · 只重写 message_start 事件的 model 字段
            current_event: bytes | None = None
            for raw_line in r.iter_lines():
                if not raw_line:
                    continue
                _tl("gw_chunk", req_id=req_id, protocol="anthropic", line=raw_line)
                emitted_any = True
                if raw_line.startswith(b"event:"):
                    current_event = raw_line.split(b":", 1)[1].strip()
                    handler.wfile.write(raw_line + b"\n")
                elif raw_line.startswith(b"data:") and current_event == b"message_start":
                    payload_data = raw_line[5:].strip()
                    try:
                        evt = json.loads(payload_data)
                        if isinstance(evt.get("message"), dict):
                            evt["message"]["model"] = model_name
                        rewritten = b"data: " + json.dumps(evt, ensure_ascii=False).encode("utf-8")
                        handler.wfile.write(rewritten + b"\n\n")
                        handler.wfile.flush()
                    except (json.JSONDecodeError, TypeError, ValueError):
                        handler.wfile.write(raw_line + b"\n\n")
                        handler.wfile.flush()
                    current_event = None
                elif raw_line.startswith(b"data:"):
                    handler.wfile.write(raw_line + b"\n\n")
                    handler.wfile.flush()
                    current_event = None
                else:
                    handler.wfile.write(raw_line + b"\n")
            return True

    attempts = 0
    retry_attempts = 0
    while True:
        attempts += 1
        try:
            ok = _run_upstream()
            if ok:
                break
            if retry_attempts >= MAX_RETRIES:
                _log(f"[ERROR] anthropic retryable status 超过 {MAX_RETRIES} 次 · 放弃 · last={last_error_status} body={last_error_body[:500]}")
                emitted_any = True
                msg = f"[dcc-proxy] anthropic {format_upstream_error(last_error_status, last_error_body, retries=retry_attempts)}"
                handler.wfile.write(emit_error(msg))
                handler.wfile.flush()
                break
            retry_attempts += 1
            delay = compute_backoff(retry_attempts, retry_after_hint)
            _log(f"[WARN] anthropic upstream retryable · attempt={retry_attempts}/{MAX_RETRIES} · sleep={delay:.1f}s · retry_after={retry_after_hint}")
            time.sleep(delay)
            continue
        except _STREAM_RETRY_EXC as e:
            if not emitted_any and attempts <= STREAM_MAX_RETRIES:
                _log(f"[WARN] anthropic stream break before first byte · {type(e).__name__}: {e} · retry {attempts}/{STREAM_MAX_RETRIES}")
                continue
            _log(f"[ERROR] anthropic stream break after emit(attempts={attempts}) · {type(e).__name__}: {e}")
            try:
                handler.wfile.write(emit_error(f"[dcc-proxy] anthropic stream broke: {type(e).__name__}"))
                handler.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            break
        except requests.RequestException as e:
            _log(f"[ERROR] anthropic gateway connect failed: {e}")
            try:
                handler.wfile.write(emit_error(f"[dcc-proxy] anthropic gateway connect: {e}"))
                handler.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            break

    _log(f"← done anthropic · dt={time.time()-t0:.1f}s")
    _tl("done", req_id=req_id, protocol="anthropic", emitted_any=emitted_any, dt=round(time.time() - t0, 3))


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _cleanup_dumps_old(dumps_dir: Path, days: int) -> None:
    """清 dumps/ 下超过 N 天的 req_YYYYMMDD_*.json 文件。

    语义与 traffic_log._cleanup_old 一致:保留 N 天窗口 = [D-N+1 .. D]。
    """
    if not dumps_dir.exists() or days <= 0:
        return
    try:
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=days - 1)).strftime("%Y%m%d")
    except (ValueError, OverflowError):
        return
    for p in dumps_dir.iterdir():
        if not p.is_file() or not p.name.startswith("req_"):
            continue
        # req_YYYYMMDD_HHMMSS_...json → 取 YYYYMMDD
        parts = p.name.split("_")
        if len(parts) < 2 or len(parts[1]) < 8 or not parts[1][:8].isdigit():
            continue
        if parts[1][:8] < cutoff:
            try:
                p.unlink()
            except OSError:
                pass


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: proxy_server.py <config.json>", file=sys.stderr)
        sys.exit(2)
    cfg_path = Path(sys.argv[1])
    if not cfg_path.exists():
        print(f"config not found: {cfg_path}", file=sys.stderr)
        sys.exit(2)
    global CONFIG, DUMP_DIR, TL, EL
    CONFIG = Config(json.loads(cfg_path.read_text(encoding="utf-8")))
    DUMP_DIR = cfg_path.parent / "dumps"
    log_dir = cfg_path.parent / "log"
    exec_log_dir = cfg_path.parent / "exec_log"
    # 覆盖顺序:env > config.json > 默认 3
    _cfg_data = json.loads(cfg_path.read_text(encoding="utf-8"))
    retention_days = int(os.environ.get(
        "DCC_LOG_RETENTION_DAYS",
        str(_cfg_data.get("retention_days", 3)),
    ))
    TL = TrafficLog(log_dir, retention_days=retention_days)
    EL = ExecLog(exec_log_dir, retention_days=retention_days)
    # dumps/ 是 400 请求 body 落盘 · 命名 req_YYYYMMDD_HHMMSS_*.json
    _cleanup_dumps_old(DUMP_DIR, retention_days)
    server = ThreadingHTTPServer(("0.0.0.0", CONFIG.port), ProxyHandler)
    _log(f"========== dcc-proxy version={DCC_VERSION} ==========")
    _log(f"dcc-proxy listening on 0.0.0.0:{CONFIG.port} · models={list(CONFIG.models_by_name)}")
    _log(f"dump dir = {DUMP_DIR}(遇 400 时落盘请求 body,滚动保留 {DUMP_KEEP} 份)")
    _log(f"traffic log dir = {log_dir}(按天 + 10MB 拆分,daemon 线程异步刷盘)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("KeyboardInterrupt, shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
