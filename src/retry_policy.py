"""HTTP 状态码分类 + 重试退避策略。参 CCR retry-policy.ts + failure-classifier.ts。

用途:proxy_server.py 首帧前遇 429 / 5xx / 408 / 409 时判断是否重试 · 计算 sleep 秒数。
首帧后(已 emit_any)不重试 · SSE 中断不可恢复。
"""
from __future__ import annotations

import json
import time


BASE_SECONDS = 1.0
CAP_SECONDS = 30.0
MAX_RETRIES = 3

# 幂等且非致命 · 允许 retry
_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

# 京东网关业务码 → 人类可读标签(429 语义细分)
# 抓自 known-issues.md · code=2007 配额用尽(日限额)· code=2008 限流(QPS/burst)
_GATEWAY_CODE_LABEL = {
    2007: "日 token 配额用尽 · 明日恢复",
    2008: "触发 API Key 限流阈值 · 请求次数/QPS 超限",
}


def is_retryable_status(status: int) -> bool:
    return status in _RETRYABLE_STATUS


def compute_backoff(attempt: int, retry_after: str | None = None) -> float:
    """指数退避 · 遵守 Retry-After header。

    attempt 从 1 开始(第 1 次重试)· sleep = min(cap, base * 2^(attempt-1))。
    Retry-After 若存在且合法 · 优先用其值(仍不超过 cap · 但 429 场景可能超 cap,
    这里遵守 CCR 做法:允许最多 60s 让开配额)。
    """
    if retry_after:
        try:
            v = float(retry_after.strip())
            if v > 0:
                return min(v, 60.0)
        except (TypeError, ValueError):
            pass
    delay = BASE_SECONDS * (2 ** max(0, attempt - 1))
    return min(delay, CAP_SECONDS)


def sleep_backoff(attempt: int, retry_after: str | None = None) -> float:
    """睡一次 · 返回实际 sleep 秒数(便于日志)。"""
    d = compute_backoff(attempt, retry_after)
    time.sleep(d)
    return d


def format_upstream_error(status: int, body_preview: str, retries: int = 0) -> str:
    """把上游错误响应转成给 CC 前端看的可读消息 · 首选解析出 error.code + error.message,
    加上业务码人类标签(如 2007 = 配额用尽)。

    输出格式(单行):
      upstream {status} [code=2007 · 日 token 配额用尽 · 明日恢复] {message} · retries=3

    解析失败时降级为原样 body_preview(截断 300)· 保证不吞信息。
    """
    parts = [f"upstream {status}"]
    parsed = None
    try:
        obj = json.loads(body_preview)
        if isinstance(obj, dict):
            err = obj.get("error")
            if isinstance(err, dict):
                parsed = err
            elif isinstance(err, str):
                parsed = {"message": err}
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    if parsed:
        code = parsed.get("code")
        msg = parsed.get("message") or ""
        cause = parsed.get("cause")
        # 有些网关把内层 error 包在 cause 字符串里(如 responses 400)· 剥一层
        if isinstance(cause, str):
            try:
                inner = json.loads(cause)
                if isinstance(inner, dict):
                    inner_err = inner.get("error") if isinstance(inner.get("error"), dict) else inner
                    msg = msg or inner_err.get("message") or ""
                    code = code if code is not None else inner_err.get("code")
            except (json.JSONDecodeError, TypeError, ValueError):
                # cause 是普通描述字符串 · 直接当消息补充
                if not msg:
                    msg = cause[:300]
        label = _GATEWAY_CODE_LABEL.get(code) if isinstance(code, int) else None
        code_seg = ""
        if code is not None:
            code_seg = f"[code={code}"
            if label:
                code_seg += f" · {label}"
            code_seg += "]"
            parts.append(code_seg)
        if msg:
            parts.append(str(msg)[:300])
        if not code and not msg:
            # parsed 存在但既无 code 也无 message · 降级为原文
            parts.append(body_preview[:300])
    else:
        # 降级:原文附上
        parts.append(body_preview[:300])

    if retries:
        parts.append(f"retries={retries}")
    return " ".join(parts)
