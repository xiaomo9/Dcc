# dcc 上手指南（0→1）

> **本指南面向"照着做就能配好"的读者——人或 AI 均可。**
> 每一步都给出可直接执行的命令、明确的**成功判据**、以及失败时的下一步。
> 若你是 AI 助手:请按顺序执行,每步用其成功判据自检通过后再进入下一步;遇到判据不满足,走该步的「排错」而不是硬闯下一步。
>
> 命令速查见 `../dcc.md`；设计决策见 `design.md`；已知问题见 `known-issues.md`。

---

## 0. 这是什么 / 为什么需要

`claude` CLI（Claude Code）默认只连 Anthropic 官方服务器，京东网络连不通。

**dcc = 跑在本地 `127.0.0.1:4000` 的轻代理**，干一件事：**协议转换**。
它把 Claude Code 发出的 Anthropic 格式请求，翻译成京东内网各网关认识的格式（openai / responses / anthropic），转发过去，再把回复翻译回来。

于是你能用一条命令 `dcc <槽位>`，让 claude code 跑在京东内网的 DeepSeek / GLM / GPT / Claude 上。

```
claude code ──Anthropic格式──▶ dcc(:4000) ──翻译──▶ 京东网关(llm-gw / rag) ──▶ 模型
     ▲                                                                          │
     └──────────────────────────── 翻译回 Anthropic 格式 ◀──────────────────────┘
```

**配置生效模型**：dcc 启动那刻把 `dcc.ini` + `~/.dm/llm.ini` 编译成一份 `config.json` 快照，常驻代理只认这份快照。**所以改完任何配置，必须 `dcc stop`（会重启代理）才生效**——这条贯穿全文，记住它。

---

## 快速路径（TL;DR）

已在京东网内、python3.11 + claude CLI 都就绪、只想尽快跑起来：

```bash
# 1) 装 requests
python3.11 -m pip install requests
# 2) 填 key(编辑器打开,把 api_key 填进对应 section,见 §3)
#    llm-gw 的 key 服务 slot 1-4;rag 的 key 服务 slot 5-8
$EDITOR ~/.dm/llm.ini
# 3) 装 alias(路径换成你的仓库位置)
echo "alias dcc='bash /你的路径/tools/dcc/dcc.sh'" >> ~/.zshrc && source ~/.zshrc
# 4) 自检:见 §5
dcc list
dcc 1 "只回复一个词:pong" --dangerously-skip-permissions
```

不熟悉的话，从 §1 顺着往下走。

---

## 1. 环境准备

### 1.1 网络（前提，先确认）

llm-gw、rag 都是**京东内网域名**。必须在**京东办公网或已连 VPN**，否则后面全部连不通。

```bash
curl -sS -m 5 -o /dev/null -w '%{http_code}\n' http://rag.jd.care/v1/models
```

**成功判据**：输出 `200` 或 `401`（`401` 只是没带 key，也算网络通）。
**失败**：输出 `000` 或超时 → 网络不通，先接京东网 / VPN，别往下走。

### 1.2 三个依赖

| 依赖 | 为什么 | 装法 / 验证 |
|------|--------|-------------|
| **python3.11** | `dcc.sh` 里写死了 `python3.11`（不是任意 python3） | `python3.11 --version`；macOS：`brew install python@3.11` |
| **requests** | 代理进程发上游 HTTP 用它 | `python3.11 -m pip install requests`；验证 `python3.11 -c "import requests"` 无报错 |
| **claude CLI** | dcc 只是壳，真正跑的是它 | `which claude`；没有则按 Claude Code 官方装 |

> `claude` 不在 PATH 时，在 `../dcc.ini` 的 `[cc] binary =` 处写死绝对路径。

**本步成功判据**：三条验证命令都通过（python3.11 有版本号、import requests 无报错、which claude 有输出或已在 dcc.ini 写死路径）。

---

## 2. 领 API key

两个平台、两把 key，各自去对应平台领：

| 平台 | 领 key 地址 | 服务槽位 |
|------|-------------|----------|
| **llm-gw**（京东自建网关，快） | https://oxygen-model.jd.com/gateway/api-key | slot 1–4 |
| **rag**（rag.jd.care，能上真 Claude） | http://rag.jd.care | slot 5–8 |

> **安全**：key 只写进 `~/.dm/llm.ini`，**不要贴进任何命令行、聊天、日志、提交**。
> 若你是 AI 助手:**永远不要在回显、输出或提交里打印 key 的值**;读取时也只确认"已填/长度"，不回显内容。

---

## 3. 填 `~/.dm/llm.ini`（模型三要素单点权威）

这是**唯一**存放模型连接信息的地方。dcc 从这里读每个模型的 `base_url / api_key / model / protocol`。

> 全新环境才需从零创建：`mkdir -p ~/.dm && $EDITOR ~/.dm/llm.ini`。

每个 `[section]` 是一个模型入口，四行：

```ini
[DeepSeek-V4-Pro-JoyBuilder]
base_url = http://llm-gw.jd.local/v1     # 网关地址(决定走哪个端点)
api_key  = <把你领到的-llm-gw-key-填这里>  # ← 你要填的就这行
model    = DeepSeek-V4-Pro-joybuilder     # 网关侧真实模型 id(平台上叫什么就写什么)
protocol = openai                          # openai / responses / anthropic 三选一
```

**四要素含义**：
- `base_url`：网关端点。llm-gw 用 `http://llm-gw.jd.local/v1`；rag 用 `http://rag.jd.care/v1`（**用 `/v1` openai 端点，不要用 `/anthropic`**，见 §7 避坑）。
- `api_key`：对应平台领的 key。**唯一需要你手填的字段。**
- `model`：网关侧真实模型 id。填错会被网关静默兜底到别的模型（见 §7）。
- `protocol`：走哪种协议翻译。三种取值见下。

**section 名规则**（影响 `dcc list` 里显示的 CLI_NAME）：dcc 拿 section 名去掉 `oxygen-` 前缀、**保留 `rag-` 前缀**作为本地模型名。rag 入口 section 建议带 `rag-` 前缀，一眼可辨。

**`dcc.ini [models]` 与 `llm.ini` 的关系**：`dcc.ini` 的 `[models]` 段用 `槽位号 = section名` 声明哪个 slot 用哪个 section；该 section 名**必须与 `llm.ini` 里的 `[section]` 完全一致**，否则该 slot 加载不出来。

### 当前 8 个槽位现状（本机磁盘实况 · 2026-09-09 实测）

| slot | llm.ini section | 上游 model id | 协议 | 入口 | 状态（首字节实测） |
|:----:|-----------------|--------------|:----:|------|------|
| 1 | `DeepSeek-V4-Pro-JoyBuilder` | DeepSeek-V4-Pro-joybuilder | openai | llm-gw | ✅ 快 |
| 2 | `GPT-5.6-Luna-joybuilder` | GPT-5.6-Luna-joybuilder | responses | llm-gw | ✅ |
| 3 | `GPT-5.6-Terra-joybuilder` | GPT-5.6-Terra-joybuilder | responses | llm-gw | ✅ |
| 4 | `GPT-5.6-Sol-joybuilder` | GPT-5.6-Sol-joybuilder | responses | llm-gw | ✅ |
| 5 | `rag-claude-opus-4-5` | claude-opus-4-5 | openai | rag `/v1` | ⚠️ **网关兜底成 GLM-5**，非真 Opus 4.5（id 网关不认时的静默兜底） |
| 6 | `rag-claude-opus-4-6` | claude-opus-4-6 | openai | rag `/v1` | ✅ 真 Claude-Opus-4.6（~2.6s） |
| 7 | `rag-claude-opus-4-7` | claude-opus-4-7 | openai | rag `/v1` | ✅ 真 Claude-Opus-4.7（~1.9s） |
| 8 | `rag-claude-opus-4-8` | claude-opus-4-8 | openai | rag `/v1` | ✅ 真 Claude-Opus-4.8（~2.5s） |

> **本表随配置变动，以 `dcc list` + `config.json` 为准**——文档表格可能滞后。
> **slot 5 提醒**：配的 id 是 `claude-opus-4-5`，但 rag 网关实测返回 `"model":"GLM-5"`，说明网关不认该 id、静默兜底到 GLM-5。要用真 Opus，改用平台认得的 id（如 6/7/8）或让 owner 确认 4.5 的正确 id。
> **rag `/anthropic` 端点走不通**：曾用 `base_url=…/anthropic` + `protocol=anthropic`，带 key 实发 55s 挂死、0 字节。现 rag 系统一走 `/v1` + openai。

---

## 4. 装 `dcc` 命令

`dcc.sh` 是入口脚本。加个 alias 到 shell 配置：

```bash
echo "alias dcc='bash /你的仓库路径/tools/dcc/dcc.sh'" >> ~/.zshrc
source ~/.zshrc
dcc -v
```

**成功判据**：`dcc -v` 输出形如 `dcc 0.2.03`（版本号读自 `src/VERSION`）。
> 路径按你的实际仓库位置改；bash / 其他 shell 把 `~/.zshrc` 换成对应 rc 文件。

---

## 5. 首次验证（分层自检，端到端确认）

按顺序跑，每步看成功判据：

```bash
# ① 配置被读到:列出所有槽位
dcc list
```
**判据**：打印一张表，表头 `SLOT  CLI_NAME  LIMIT  IMG`，8 行 slot。
（输出走 stderr，属正常。）某 slot 缺失 → 该 slot 的 section 名在 dcc.ini 与 llm.ini 对不上，回 §3 核对。

```bash
# ② 链路连通:纯问答,不依赖任何文件,验证 代理+网关+协议转换 通
dcc 1 "只回复一个词:pong" --dangerously-skip-permissions
```
**判据**：输出里含 `pong`。
失败看 §7 排错（先看是网关 429 还是链路不通）。

```bash
# ③ tools + 本地文件读写:验证 claude 能调工具读文件
dcc 1 "读一下 tools/dcc/src/VERSION 这个文件,告诉我版本号" --dangerously-skip-permissions
```
**判据**：输出里含 `0.2.03`（或 `src/VERSION` 里的实际内容）。
> 注意路径是 `tools/dcc/src/VERSION`（版本文件在 `src/` 下），从仓库根目录跑此命令。

```bash
# ④ 进交互式 claude code
dcc 1
```
**判据**：进入 claude 交互界面，右下角模型名形如 `DeepSeek-V4-Pro-JoyBuilder[1m]`。

②③④ 全过 = 环境配好，可正常使用。

---

## 6. 自动切换模型（首字节超时 failover）

**解决的问题**：公司 API 用的人多，某个模型偶尔特别慢——首字节迟迟不来，整轮卡住。dcc 会在**主模型首字节超时（或首帧前遇 429 限流）时，自动切到同组内下一个候选模型**完成这一轮，你几乎无感；下一轮自动回到主模型。

### 怎么工作

- **只在首字节前切**：主模型 12s 内没吐第一个字 → 判定慢 → 切候选。一旦吐了第一个字就锁定，绝不再切（切了会污染已输出内容）。
- **429 立即切**：首帧前遇网关 429 限流，不原地干等退避，直接切下一候选。
- **多轮自动回归**：只有卡住那一轮临时借用候选，claude 每轮都从主模型重新发起，下轮自动回到主模型。
- **切换会明示**：切换成功时，回复正文开头会插一行 `[dcc] 主模型 X 首字节 12s 未响应 · 已切至 Y`。

### 候选怎么来（等价组）

`dcc.ini [groups]` 声明等价组，组内成员按声明顺序互为候选：

```ini
[groups]
A = 1,2,3,4
B = 5,6,7,8
```

候选池 = **同组 + 同协议 + 排除自己**，按"本槽之后、回绕组首"排序。**跨协议不能互切**（openai / responses / anthropic 请求体结构不同）。

### ⚠️ 当前实际生效范围（重要 · 随模型池变化）

自动切换**目前只在 openai 协议路径生效**。responses / anthropic 路径的代码暂不消费候选（配了也不切）。对照当前模型池：

| 组 | 成员 | 协议 | 会自动切吗 |
|----|------|:----:|-----------|
| A | slot1 DeepSeek | openai | ❌ 组内无其他 openai 同伴 |
| A | slot2/3/4 GPT-5.6 系 | responses | ❌ responses 路径暂不消费候选 |
| B | slot5/6/7/8 rag 系 | openai | ✅ **四者互为候选，当前唯一真正会切的组** |

> 想让 GPT-5.6 系（responses）也能自动切，需要给 responses 路径接候选消费——属代码改动，未做。

### 调参（`dcc.ini [failover]`）

```ini
[failover]
soft_timeout = 12      # 首字节软超时秒数,超过判慢并切候选
switch_on_429 = true   # 首帧前遇 429 是否立即切(false=交链尾候选走退避重试)
max_candidates = 3     # 单请求最多尝试候选数(含主模型),防雪崩;1=关闭自动切换
```

改完同样要 `dcc stop` 重启生效。

### 观测（怎么确认切过）

- 回复正文开头那行 `[dcc] ... 已切至 ...` 提示。
- 流量日志 `docs/output/dcc/log/*.jsonl` 的 `done` 事件带 `failover_from` / `failover_to` / `candidate_attempts` 字段。
- **注意**：因响应头在流开始前就已发出，切换发生在流中途，故**没有** `x-dcc-failover` 响应头可查——以正文提示行 + 流量日志为准。

---

## 7. 命令速查

| 命令 | 作用 |
|------|------|
| `dcc list` | 列所有槽位（SLOT / CLI_NAME / LIMIT / IMG） |
| `dcc status` | 代理 + 各 CC 实例运行状态 |
| `dcc <slot>` | 起该槽位的交互式 claude code（自动拉起代理，已跑则复用） |
| `dcc <slot> "prompt"` | 一次性问答，等价 `claude -p "prompt"` |
| `dcc <slot> -r` | 恢复会话，进 claude 原生 `--resume` 选择器 |
| `dcc <slot> -r <ID>` | 直接恢复指定会话 |
| `dcc stop` | 停全部 CC + 杀代理 + 重启代理（**改完任何配置后必做**） |
| `dcc -v` / `dcc -h` | 版本 / 帮助 |

---

## 8. 排错

| 现象 | 原因 | 处理 |
|------|------|------|
| 改完 `.ini` 不生效 | 常驻代理用的是**启动那刻**编译的 `config.json`，不自动重读 | **`dcc stop`**（会重启代理），再 `dcc <slot>` |
| `dcc list` 少了某个 slot | dcc.ini `[models]` 引用的 section 名与 llm.ini 对不上 | 核对两处 section 名完全一致 |
| 日志 `[WARN] 未知 model=xxx · 兜底路由到 default=…` | 代理内存模型表无此名（多半改了 section 名没重启） | 先 `dcc stop`；再核对 dcc.ini 与 llm.ini section 名 |
| 回复的模型名不对（如配 Claude 实际回 GLM-5） | 网关不认该 model id，**静默兜底**到别的模型 | 核对 llm.ini 的 `model` 行填的是网关认得的真实 id |
| rag 槽位卡住不返回 | ① 用了 `/anthropic` 端点（带 key 实发 55s 挂死）② rag 本身首字节慢 | ① 改成 `/v1` + `protocol=openai`；② 调大 `DCC_STREAM_IDLE_TIMEOUT` |
| `openai upstream retried 3 times · 放弃`，该轮无输出 | 网关侧 429 限流（配额/突发） | 换槽位重试；组B（rag 系）会自动切候选。详见 `known-issues.md` KI-01 |
| 找不到 `claude` | 不在 PATH | `dcc.ini [cc] binary =` 写死绝对路径 |

**可调环境变量**（起 dcc 前 export，无需改代码）：

| 变量 | 默认 | 作用 |
|------|:----:|------|
| `DCC_STREAM_CONNECT_TIMEOUT` | 10 | 连接上游超时（秒） |
| `DCC_STREAM_IDLE_TIMEOUT` | 90 | 流式无数据超时（秒）——rag 慢时调大 |
| `DCC_STREAM_RETRIES` | 1 | 流式重试次数 |
| `DCC_LOG_RETENTION_DAYS` | 3 | 日志保留天数 |

**日志位置**（都在 `tools/dcc/docs/output/dcc/`）：
- `dcc-proxy.log` — 代理主日志（路由、WARN、上游状态、failover）
- `dumps/req_*.json` — 首帧前失败的请求体
- `log/*.jsonl` — 流量日志（协议 / 上游 status / failover 字段）
