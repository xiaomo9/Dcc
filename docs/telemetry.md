# dcc 使用统计

客户端已实现，服务端和 ai-x 看板尚未接入。默认关闭，不会向现有平台发送数据。

## 开启方式

修改仓库 `dcc.ini`：

```ini
[telemetry]
enabled = true
endpoint =
```

`enabled=true` 且 endpoint 留空时，只记录本地待发送事件。确认服务端支持下述协议后，
将 endpoint 填为采集接口完整 URL（优先 HTTPS）。`enabled=false` 时不记录也不发送。
关闭不会删除已有队列；重新开启后可能补发保留期内的事件。
分发给使用者时应说明采集字段和关闭方式。

配置随代理启动加载。已有代理会复用旧配置；更改后需要重启代理。
注意 `dcc stop` 会停止全部受管 Claude Code 实例并重启代理，请在会话可中断时执行。

## 上报协议

`POST endpoint`，Content-Type 为 application/json：

```json
{
  "batchId": "uuid",
  "sentAt": 1780000000000,
  "events": [{
    "id": "uuid",
    "clientId": "persistent-random-uuid",
    "toolName": "dcc",
    "extVersion": "0.2.03",
    "eventName": "dcc_launch",
    "timestamp": 1780000000000,
    "properties": {"slot": "1", "protocol": "openai", "mode": "interactive"}
  }]
}
```

沿用已有平台的 `extVersion` 字段表示 dcc 版本。时间戳为毫秒。
HTTP 2xx 且响应 JSON 的 code 缺省、0 或 "0" 视为成功；空响应也接受。
服务端应原子接受整批事件，按 `id` 去重，并通过 `toolName` 隔离不同工具统计。
客户端不会跟随重定向。目前没有新增鉴权逻辑，需与服务端后续约定。

## 事件及统计口径

| 事件 | 字段及含义 |
| --- | --- |
| dcc_launch | slot、protocol、mode（interactive/prompt/resume）；有效槽位的启动尝试，不代表 Claude Code 已成功启动 |
| dcc_proxy_start | result（ready/failed）、durationMs；启动或复用代理的结果 |
| dcc_request_done | requestId、slot、protocol、model、stream、success、failReason、durationMs、firstEventMs、inputTokens、outputTokens、usageSource、candidateCount、switchCount |
| dcc_model_failover | requestId、protocol、fromModel、toModel、reason；切换到下一候选时记录，不代表该候选最终成功 |

请求事件每个已解析且进入模型路由的 `/v1/messages` 请求记录一次；
不统计健康检查、count_tokens、非法 JSON 或未知路径。
slot 是初始路由槽位，model 是最后尝试的上游模型；candidateCount 不包括同模型内部重试。
请求成功必须观察到对应协议完成信号，且没有最终 HTTP/连接/流事件错误或客户端断开。
OpenAI 使用 finish_reason，Responses 使用 response.completed，Anthropic 使用 message_stop。
EOF 前无完成信号记为 incomplete_stream，不能用代理返回 HTTP 200 代替成功状态。
firstEventMs 是请求开始至首个可解析上游 SSE data 事件的时间，包含候选切换等待，
不是 TCP 首字节或首个文本 token 耗时。未收到有效事件时为 null。
Token 仅记录上游实际返回值，usageSource=upstream；未提供则省略，不冒用估算值或零值。

## 存储和发送

- 队列位于配置的 output.dir 下 `telemetry/telemetry.sqlite3`，与全流量日志分离。
- clientId 首次生成并持久保存于该数据库，代表这份本地安装；删除数据库会产生新 ID，不能当作真实人数。
- SQLite 串行化多进程写入；CLI 同步落盘，避免 exec 替换时丢失内存事件。锁等待上限 100ms，失败放弃当前事件，不阻断业务。
- 常驻代理后台线程启动时发送，随后每 30 秒最多发送 20 条；失败退避至最多 300 秒。连接/读取超时分别为 2/4 秒。
- 最多保留最新 200 条，记录/发送时删除超过 7 天的事件。队列满、磁盘异常或锁竞争可能丢事件，因此不是审计日志。
- 发送成功仅删除本批事件，发送期间新增的事件保留。崩溃可能导致同 ID 重发，依赖服务端幂等去重。
- 代理未运行时仅积压，下一次代理启动补发；关闭代理不等待上传。

不采集 ERP、用户名、prompt、模型回复、工具参数、路径、API key 或原始错误文本。
现有 traffic log/dumps 行为不变，不会被这个模块整体上传。

## 本地验证

```bash
python3.11 -m unittest discover -s tests
```

测试仅使用临时 SQLite 和 127.0.0.1 模拟接口，不连接内部平台。
