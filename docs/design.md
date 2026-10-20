# dcc 设计决策与变更历史

> 本文件记录 dcc 的设计决策、关键变更和踩坑纪要。用法见 `dcc.md`。

## 关键变更

### 2026-10-20 · 切换条件与软超时连接回收

OpenAI 和 Responses 的候选守卫现在只在上游返回可重试状态且 `switch_on_429=true` 时切换；400 等不可重试错误直接返回。`switch_on_429=false` 时，429 留在原模型退避重试，守卫请求计入原有 `MAX_RETRIES + 1` 次总请求预算。首字节软超时后标记守卫请求已取消，关闭尚未移交的连接；若连接稍后才建立，由工作线程立即关闭，避免迟到响应留在后台。

无监听端口的检查已验证 OpenAI / Responses 对 400、关闭切换的 429、开启切换的 429 的分支和请求次数；连接回收与移交竞态测试、流转换测试通过。完整本地 HTTP 测试因本次自动审批额度限制未能运行，真实网关与 Claude Code 未实测。

### 2026-10-20 · 上游错误返回与多会话实例管理

**上游错误返回**：OpenAI、Responses 原先把部分失败写进 assistant 正文；非流式聚合也会把 Anthropic 的错误事件转成普通消息。现在由 `UpstreamFailure` 保存状态码、错误类型与消息，在 HTTP 出口统一返回：流式发送 SSE `error`，非流式返回错误 JSON。上游 HTTP 错误保留状态码；无上游错误状态时，超时映射 504，连接失败、截断及流内错误映射 502。终止失败不再补发正常结束事件，已输出内容不重放。重试预算、候选顺序和切换条件保持不变。

**多会话实例**：`instances.json` 从槽位键改为 PID 字符串键，保留 `slot`、模型、目录和启动时间等字段。同槽位可登记多个实例。读取时兼容旧格式，下一次写入保存新格式；旧版已经覆盖丢失的记录无法恢复，不保证新旧版本并发写入兼容。独立锁文件配合 `fcntl.flock` 保护读取、清理、更新和写入，同目录唯一临时文件加原子替换避免半截 JSON。

`status` 展示全部已登记的存活实例；内部按槽位停止会处理该槽位的全部实例，`stop_all` 按实例统计成功发送的信号数。保留自身保护，信号发送失败不删除记录。`dcc stop` 仍执行停止 CC、停止代理、重启代理，不新增命令。存活判断沿用 PID 检查，本次未增加进程身份验证。

**验证**：本次实现后执行 `python3.11 -m unittest discover -s tests -v`，19 项测试通过。包括本地 HTTP 模拟上游的三协议流式/非流式失败、429 重试耗尽、连接异常、超时、截断、流内错误、首帧后不切换及成功候选不重放；实例测试覆盖同槽位多实例、6 个进程并发登记、旧格式兼容、死亡记录清理、停止失败、自身保护和写入失败保留原记录。原有工具调用、thinking、切换与统计测试通过。

**验证边界**：没有连接真实网关验证，也没有实测真实 Claude Code 的错误展示和客户端重试行为；本地测试不等同于这些端到端验收。本次实现与验证未重启正在运行的代理。

### 2026-08-18 · rag 系改走 anthropic 原生协议

rag 系(slot 6-8)从 openai/responses 协议改走 **anthropic 原生 `/v1/messages`** — rag 网关直接支持 Anthropic SSE + tool_use,tools 端到端全通(相比原 openai 端网关不透传 tool_calls 的问题彻底解决)。

> **⚠️ 2026-09-09 更正**:上述「rag 网关直接支持 Anthropic SSE」在当前环境**已不成立**。
> 实测 `http://rag.jd.care/anthropic/v1/messages` 带 key 实发 55s 挂死、0 字节(详见 known-issues KI-02)。
> 现 rag 系(6/7/8)统一走 openai 端点。tool_use 若有需要另行验证 openai 端的透传。

### 2026-08-19(v0.1.15)· 修 stream=false bug

`_handle_messages` 之前无条件回 SSE,CC 内部非流式重试(slot 上游 429 后触发)会收到 event-stream body 报错"non-streaming request was answered with a stream"。现在按 body.stream 分流:True 保 SSE;False 用 `_BufWFile` 缓冲三条 stream_*_to_anthropic 的 SSE 输出,`collect_sse_to_message` 聚合成 anthropic Message JSON(Content-Type: application/json)一次性回。openai/responses/anthropic 三协议 × true/false 六组 curl 验证通过。

### 2026-08-19(v0.1.16)· 图文能力守卫

`~/.dm/llm.ini` 每 section 支持 `supports_images = true|false`(缺省 false 保守)· `false` 时 proxy 层剥掉历史 `image`/`image_url` block(避免 GLM/DeepSeek 等纯文本模型收到图片直接 400)· 剥离后:openai/responses 分支 SSE 首帧插入 `[dcc] ⚠️ 本 slot 不支持图文 · 已剥离 N 张历史图片…` 明文提示,anthropic 分支通过响应头 `x-dcc-warn-images-stripped: N` + proxy log WARN 提示。要开图文能力的 slot,在对应 llm.ini section 加 `supports_images = true`(先确认模型确实支持 vision)。
