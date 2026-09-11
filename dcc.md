# dcc — Distribute CC 本地多模型代理管理器

一句话:本地轻代理做协议转换 · 一条命令用京东内网 DeepSeek / GLM / GPT / Claude 启 claude code。

## 用法

```bash
dcc list                    # 列所有已配 slot(dcc.ini [models])
dcc status                  # 代理 + 各 CC 实例状态
dcc 1                       # 起 slot=1 的 CC(自动起代理 · 已跑则复用)
dcc 1 "prompt"              # 一次性问答(等价于 claude -p "prompt")
dcc 2 -r                    # 恢复会话(进入 claude 原生 --resume 选择器)
dcc 2 -r 7952ebc2...        # 直接恢复指定会话(等价于 claude --resume <ID>)
dcc stop                    # 停全部 CC + 杀代理 + 重启(改完 dcc.ini 后必做)
dcc -v                      # 打印版本号
```

## 安装
让AI阅读[text](getting-started.md)文件并执行相关指令，一键配置


配好先自检:①链路 `dcc 1 "只回复一个词:pong" --dangerously-skip-permissions`(输出含 pong);②tools+读文件 `dcc

## 模型池（本机磁盘实况 · 以 `dcc list` 为准）

| slot | model id | protocol | 入口 |
|------|----------|----------|------|
| 1 | DeepSeek-V4-Pro-joybuilder | openai | llm-gw |
| 2 | GPT-5.6-Luna-joybuilder | responses | llm-gw |
| 3 | GPT-5.6-Terra-joybuilder | responses | llm-gw |
| 4 | GPT-5.6-Sol-joybuilder | responses | llm-gw |
| 5 | claude-opus-4-5 | openai | rag `/v1` |
| 6 | claude-opus-4-6 | openai | rag `/v1` |
| 7 | claude-opus-4-7 | openai | rag `/v1` |
| 8 | claude-opus-4-8 | openai | rag `/v1` |

> slot 1-4 走 llm-gw · slot 5-8 走 rag `/v1`（openai 端点；rag `/anthropic` 带 key 实发 55s 挂死，勿用）。
> 模型池经常变，本表可能滞后——**运行时以 `dcc list` + `config.json` 为准**。

## 自动切换（首字节超时 failover）

主模型首字节 12s 未响应（或首帧前遇 429）时，自动切到**同组同协议**下一候选完成本轮，仅首帧前切、下轮自动回归。等价组见 `dcc.ini [groups]`，参数见 `[failover]`。
**当前只在 openai 路径生效**：组B（slot 5-8，rag openai）互为候选会切；组A 的 GPT-5.6 系是 responses 协议、暂不消费候选。详见 `getting-started.md` §6。

## 依赖

- `claude` CLI 在 PATH
- `python3.11 -m pip install requests`
- 两平台 API key 写入 `~/.dm/llm.ini`:llm-gw(`https://oxygen-model.jd.com/gateway/api-key`)+ rag(`http://rag.jd.care`)

## 更多

设计决策、变更历史、已知问题 → `docs/design.md` + `docs/known-issues.md`。
