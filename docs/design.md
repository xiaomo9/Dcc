# dcc 设计决策与变更历史

> 本文件记录 dcc 的设计决策、关键变更和踩坑纪要。用法见 `dcc.md`。

## 关键变更

### 2026-08-18 · rag 系改走 anthropic 原生协议

rag 系(slot 6-8)从 openai/responses 协议改走 **anthropic 原生 `/v1/messages`** — rag 网关直接支持 Anthropic SSE + tool_use,tools 端到端全通(相比原 openai 端网关不透传 tool_calls 的问题彻底解决)。

> **⚠️ 2026-09-09 更正**:上述「rag 网关直接支持 Anthropic SSE」在当前环境**已不成立**。
> 实测 `http://rag.jd.care/anthropic/v1/messages` 带 key 实发 55s 挂死、0 字节(详见 known-issues KI-02)。
> 现 rag 系(6/7/8)统一走 openai 端点。tool_use 若有需要另行验证 openai 端的透传。

### 2026-08-19(v0.1.15)· 修 stream=false bug

`_handle_messages` 之前无条件回 SSE,CC 内部非流式重试(slot 上游 429 后触发)会收到 event-stream body 报错"non-streaming request was answered with a stream"。现在按 body.stream 分流:True 保 SSE;False 用 `_BufWFile` 缓冲三条 stream_*_to_anthropic 的 SSE 输出,`collect_sse_to_message` 聚合成 anthropic Message JSON(Content-Type: application/json)一次性回。openai/responses/anthropic 三协议 × true/false 六组 curl 验证通过。

### 2026-08-19(v0.1.16)· 图文能力守卫

`~/.dm/llm.ini` 每 section 支持 `supports_images = true|false`(缺省 false 保守)· `false` 时 proxy 层剥掉历史 `image`/`image_url` block(避免 GLM/DeepSeek 等纯文本模型收到图片直接 400)· 剥离后:openai/responses 分支 SSE 首帧插入 `[dcc] ⚠️ 本 slot 不支持图文 · 已剥离 N 张历史图片…` 明文提示,anthropic 分支通过响应头 `x-dcc-warn-images-stripped: N` + proxy log WARN 提示。要开图文能力的 slot,在对应 llm.ini section 加 `supports_images = true`(先确认模型确实支持 vision)。
