# dcc — Distribute CC 本地多模型代理管理器

一句话:本地轻代理做协议转换 · 一条命令用京东内网 DeepSeek / GLM / GPT / Claude 启 claude code。

## 用法

```bash
dcc list                    # 列所有已配 slot(dcc.ini [models])
dcc status                  # 代理 + 各 CC 实例状态
dcc settings                # 将全部槽位写入 ~/.claude/settings.json，修改前备份
dcc ui                      # 打开本机配置与 Token 用量面板，Ctrl-C 关闭
dcc start                   # 只启动/复用配置端口代理，供 VS Code 面板使用
dcc reset                   # 停已登记实例和代理，再加载当前配置启动代理
dcc 1                       # 起 slot=1 的 CC(自动起代理 · 已跑则复用)
dcc 1 "prompt"              # 一次性问答(等价于 claude -p "prompt")
dcc 2 -r                    # 恢复会话(进入 claude 原生 --resume 选择器)
dcc 2 -r 7952ebc2...        # 直接恢复指定会话(等价于 claude --resume <ID>)
dcc stop                    # 停止全部已登记 CC 实例和代理
dcc -v                      # 打印版本号
```

同一槽位可以在多个终端同时启动。`dcc status` 按 PID 展示全部已登记的存活实例；`dcc stop` 向这些实例发送停止信号，并停止代理，不再自动重启。停止信号发送失败的记录会保留。

## 可视化配置面板

运行 `dcc ui`，命令会打开本机浏览器，终端保持运行；按 Ctrl-C 关闭。页面只监听随机的 `127.0.0.1` 端口，使用本次启动的访问令牌。已有 API key 默认遮挡，点击“查看”才读取明文；不要分享面板网址或截图中的密钥。

面板分为“模型入口”“槽位与切换”“用量看板”三个 Tab。前两个可管理 `~/.dm/llm.ini` 中的模型入口（新增、修改、删除、重命名）与 `dcc.ini` 中的槽位、显示名、上下文、分组和切换策略。模型入口和槽位在大屏每行各显示四张卡片；原 INI 的附加字段保存时保留，但不在面板编辑。槽位的显示名称和上下文窗口留空后保存，会删除对应覆盖项并恢复默认值。拖动槽位排序，首个有效槽位为默认模型。删除仍被槽位引用的入口会被阻止。每个模型的“测试连接”用未保存的草稿发一条简短生成请求，可能消耗少量模型用量；测试本身不写文件。若上游在测试额度内只返回推理内容，面板会报告已连通但没有可见文本；这不能代替正常长度请求的输出验证。HTTP 502 等上游错误仍显示为失败。

“用量看板”按实际上游模型展示今天、近 7／30／90 天的已知输入、输出 Token、上游调用次数及每日趋势。首次打开及以后刷新时会从现存流量日志增量补录；日志的保留期可能只有几天，旧请求也可能未返回 usage，而且旧版日志不能完整区分候选模型内部重试，因此不能代表完整历史。缺少输入或输出用量的调用标为“未知”，不按 0 估算；同一调用多次返回 usage 时取最后已知值，重试和模型切换分别归到实际模型。[Anthropic 协议](https://docs.anthropic.com/en/docs/about-claude/pricing)的缓存创建和读取 Token 计入已知输入，OpenAI 和 Responses 使用上游返回的输入总数。UI 的“测试连接”不计入。

本地统计存于输出目录的 `token_usage.sqlite3`，保留今天及之前 89 天，只保存去重标识、时间、模型标识、槽位、协议与用量数字，不保存对话正文或密钥，也不上传。它独立于默认关闭的 telemetry 上报队列。看板刷新不会保存配置或重启代理；更新本功能后运行 `dcc reset` 才能让后台代理持续记录新请求，**该命令会停止已登记的 Claude CLI 会话**，请在会话可中断时执行。

若内网 RAG 地址经系统 HTTP 代理返回 502，可在启动 `dcc ui` 和 `dcc start` 的终端将 `rag.jd.care` 加入 `NO_PROXY`（例如 `export NO_PROXY="${NO_PROXY:+$NO_PROXY,}rag.jd.care"`）。已运行的 dcc 代理不会继承后来修改的环境变量，需在没有进行中会话时重新启动；`dcc reset` 会停止已登记的 CLI 实例。

保存前页面显示将停止的已登记实例数。实际修改前会备份原 INI 文件，修改后的 `llm.ini` 仅当前用户可读写；若槽位列表或展示发生变化，也会按 `dcc settings` 的规则备份并更新 Claude 用户设置，然后重载代理。外部编辑过配置时，页面会要求重新读取，避免覆盖。保存或重载失败时会恢复原文件及原权限，并尽力恢复旧代理；**已停止的 Claude CLI 会话无法自动重新打开**。保存成功后重新打开 Claude 面板会话，才能看到更新后的模型列表。

改完代理配置可执行 `dcc reset`，效果相当于依次执行 `dcc stop`、`dcc start`。它也会停止全部已登记的终端 CLI 实例，并中断正在进行的面板请求；若配置端口仍被占用，会报错而不声称重载成功。`dcc reset` 不改写 Claude 用户设置，槽位列表变化后还需执行 `dcc settings` 并重开会话。

旧版按槽位保存的实例记录会自动按 PID 归一化，写入时保存新格式；此前被旧版覆盖丢失的记录无法恢复。更新后请统一使用新版启动终端，不保证新旧版本同时写入实例文件的兼容性。

## VS Code Claude Code 聊天面板

安装官方 Claude Code 扩展（`modelPicker` 需要 Claude Code 2.1.242 或以上），在配置好 `dcc.ini` 和 `~/.dm/llm.ini` 后执行：

```bash
dcc settings                # 写入全部模型配置，不启动代理或 Claude
dcc start                   # 启动/复用配置端口代理
```

然后重新打开 Claude 面板会话，使用模型菜单或 `/model` 选择槽位。**这会同时改变普通 `claude` 命令的连接方式**，因为它与扩展共用 `~/.claude/settings.json`；`dcc <槽位>` 仍可指定模型。命令固定更新这份用户设置；若自行设置了 `CLAUDE_CONFIG_DIR`，客户端会使用其他目录，本命令不跟随它写入。

`dcc settings` 合并本地代理地址（默认 `http://127.0.0.1:4000`）、占位认证值、默认模型和全部模型列表，并处理与代理冲突的 API key、云提供方选择变量。各上游真实 `base_url`、`api_key` 和协议继续由代理从现有配置读取，不写入 Claude 设置。权限、主题、其他环境变量及无关配置保留；自定义请求头保留原内容，仅更新 dcc 路由标记。

模型列表按 `dcc.ini [models]` 声明顺序显示 `#槽位 · 模型名`，默认取第一个有效槽位。写入命令也会重设默认模型；Claude 的显式启动参数和恢复会话模型仍可能优先。Default 行及恢复会话的当前模型行可能继续显示。面板和普通 `claude` 会发送严格路由标记，未配置的模型返回 404 `not_found_error`，需重新选择有效槽位。

内部模型 ID 为 `dcc-slot-<槽位>`（如 `dcc-slot-1`），显示标签是真实本地模型名，避免客户端隐藏旧模型；代理按槽位找到各自上游地址和密钥。继续旧会话时请保持槽位编号含义稳定；代理仍识别旧会话带后缀的模型 ID。Sonnet/Opus/Haiku 别名和辅助请求默认映射到第一个槽位。面板切换不动态应用终端启动时的逐槽位 `autoCompactWindow`，原有自动压缩设置保留。

已有设置每次实际变更前，命令会在同目录保存原文备份（`settings.json.dcc-backup-<唯一后缀>`），并打印完整路径；新文件和备份权限为 `0600`。重复执行且内容无变化时不重写、不新增备份。无有效槽位、无效 JSON、错误的根节点或 `env` 类型，以及备份失败时，命令报错并保留原文件；替换写入失败时原文件也保留。JSON 排版会重新格式化。

恢复时关闭相关 Claude 会话，将实际备份路径代入下方命令，再重新打开会话：

```bash
cp '/实际路径/settings.json.dcc-backup-唯一后缀' ~/.claude/settings.json
```

若之前文件不存在，则首次写入不生成备份；恢复时可手动移走生成的文件。备份包含原设置，请保存在本机。

代理启动与停止共用独立的 PID 锁文件，串行完成端口检查、启动就绪和 PID 写入，避免多个终端同时启动时相互覆盖记录。同一输出目录下的新版本命令共用这把锁；旧版本命令不参与锁保护。

复用代理时会比较当前配置与后台实际加载的配置摘要。配置变化时提示“后台代理仍使用旧配置”，但不会自动重启或覆盖运行配置；旧代理未提供摘要时提示“无法确认配置是否一致”。按提示执行 `dcc reset` 后再重开会话。摘要用于核对配置，不在提示中输出密钥。

`dcc start` 只启动/复用配置端口的代理；其他服务占用端口时直接报错，不换端口。若仍运行更新前的旧代理，可执行 `dcc reset` 更新。修改模型配置后，重新执行 `dcc settings`，再运行 `dcc reset` 更新代理快照并重开会话。面板由 VS Code 管理，不登记到 `instances.json`，也不计入 `dcc status` 实例数。

此方案不需要 `claudeCode.claudeProcessWrapper`。若之前配过该选项，请手动从 VS Code 用户/工作区设置中移除，再重开面板；命令不会修改 VS Code 设置。配置文件仍在原位置，继续使用已有历史和个人设置。验证范围见 [设计记录](docs/design.md)。

## 上游失败如何返回

OpenAI、Responses、Anthropic 三种协议统一处理上游 HTTP 错误、重试耗尽、连接异常和不完整的响应流：

- 流式请求：HTTP 200 已发出后，以 SSE `error` 事件结束，不再把错误写成 assistant 正文或补发成功结束事件。
- 非流式请求：返回 `type: "error"` 的 JSON 和非 2xx 状态；上游 HTTP 错误保留其状态码，无上游错误状态时，超时返回 504，连接失败、截断或流内错误返回 502。
- 已输出的内容不重放；现有重试预算、候选顺序和切换条件未调整。实际客户端可能有自己的重试行为。

实现与本地验证范围见 [设计记录](docs/design.md)；真实 Claude Code 的错误展示和重试行为尚未实测。

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
