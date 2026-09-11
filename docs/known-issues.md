# dcc 已知问题(待解决 · 未动手)

> 记录问题现场 · 供以后集中处理 · 本文件不代表 TODO,只是「留证据」。
> 触发处理需 owner 明确同意。

---

## KI-01 · openai 协议上游 429 三次退避后放弃

### 现象

CC 侧收到内嵌 assistant 消息:

```
[dcc-proxy] openai upstream retried 3 times · 放弃
```

出现后 CC 会当作正常回答继续走,导致该轮对话缺失真正的模型输出,
用户体验为"任务无声中断"。首次观察时间 2026-08-14 02:37 前后
(`/loop wakeup` 期间 dcc 2 窗口)。

### 现场

- 报错发出位置:`tools/dcc/src/proxy_server.py:930`
  ```python
  handler.wfile.write(emit_content_delta(0,
      f"[dcc-proxy] openai upstream retried {retry_attempts} times · 放弃"))
  ```
- 触发路径:`_run_upstream()` 返 False → 首帧前 · retryable status(429/5xx/408/409/425)
  → `retry_attempts >= MAX_RETRIES` 后走「放弃」分支
- 重试策略(`retry_policy.py`):
  - `MAX_RETRIES = 3`
  - `BASE = 1.0s` · `CAP = 30.0s` · 指数退避 `sleep = min(cap, base * 2^(attempt-1))`
  - `Retry-After` header 存在时优先(最大 60s)
  - 总退避窗口约 `1 + 2 + 4 = 7s`

### 上游错误特征

`tools/dcc/log`(traffic_log)最近 15 个滚动文件中 gw_status 分布:

| protocol | status | 次数 |
|----------|--------|-----|
| openai | 200 | 69 |
| openai | **429** | **16** |
| responses | 200 | 14 |

`docs/output/dcc/dcc-proxy.log` 中的 429 报文样本:

```json
{"error":{"code":2008,"message":"请求次数超过模型限流阈值"}}
{"error":{"code":2008,"message":"请求次数已超过API Key限流阈值"}}
{"error":{"cause":"{\"error\":{\"message\":\"Request rate increased too quickly. ... \",\"type\":\"limit_burst_rate\",\"code\":\"limit_burst_rate\"}}","code":429}}
```

时段特征:多个 upstream 同时段密集 429(2026-08-13 10:52 时段单分钟 20+ 次 429),
说明是网关配额 / API Key 侧限流,不是单模型故障。

### 影响

- openai 协议上游(DeepSeek-V4-Pro / GLM-5.2 / Qwen 系等)全部受影响
- responses 协议上游(GPT-5.6-* / Claude-Opus-4.7-joybuilder)走独立路径,不共享该重试计数
  但同类 429 也可能出现(2026-08-13 23:55 有 responses 协议 429 样本)
- 首帧前才触发;已 emit 的 SSE 中断走另一路径(`_STREAM_RETRY_EXC`),不进本条

### 已排除

- ❌ 不是 CC 客户端异常(重试是 dcc 主动放弃 · 有明确日志)
- ❌ 不是网络断连(那走 `_STREAM_RETRY_EXC` 分支)
- ❌ 不是模型不存在或参数错误(那会走 non-retryable 4xx · 立即透传)

### 候选方案(未决 · 待 owner 拍板)

**A. 扩大重试预算(低风险)**
- `MAX_RETRIES = 3 → 5` · `BASE = 1.0 → 2.0`
- 退避窗口 7s → 62s · 能扛住多数瞬时 429 / 短抖动
- 代价:用户等待时间变长
- **不解决**:网关整体配额耗尽的场景

**B. slot 级 fallback(改动大 · 曾被 YAGNI 打回)**
- 当前 slot 三次失败后,自动切到 llm.ini 的下一个 slot 继续
- 效果:openai 系全线 429 时能切到 responses / Claude 系
- 代价:
  - 违反 R16「禁兜底开关」的默认倾向
  - CC 使用者无感换模型 · 后续回滚定位困难
  - 上次决策(短期记忆 `20260813_dcc_p0_p2_ccr对齐首轮.md`)已经打回过
- 需要 owner 明说要不要破例

**C. 错误提示优化(低风险 · 与 A 组合)**
- 报错文案带上 upstream_id / status / 最后一次 body_preview / 建议命令
  ```
  [dcc-proxy] openai upstream {upstream_id} 429 · 重试 5 次仍失败 ·
  建议 dcc N 切换其他 slot(N = 2/3/5/...)
  ```
- 让使用者手动切模型 · 比自动 fallback 稳定可控
- 符合「单路径 + 用户决策」原则

### 处置

**本次不改**。有需要处理时:
1. owner 定方向(A / A+C / B)
2. 落工作清单 + 落短期记忆
3. 修 `tools/dcc/src/retry_policy.py` 常量 · 或修 `proxy_server.py:917-937` 循环体
4. `dcc N "hello"` 手工触发(需网关限流现场 · 否则模拟 429 用 mock server)
5. R17 关单 + commit

### 相关文件

- `tools/dcc/src/retry_policy.py` — 退避常量 + `is_retryable_status`
- `tools/dcc/src/proxy_server.py:784-937` — `_run_upstream` + 重试循环
- `docs/output/dcc/log/20260814.*.jsonl` — traffic_log 现场
- `docs/output/dcc/dcc-proxy.log` — proxy stderr 现场
- `docs/output/dcc/dumps/req_*.json` — 首帧前失败 body dump

---

<!-- 新增问题往下追加 KI-02 / KI-03 · 各段结构同 KI-01 -->
