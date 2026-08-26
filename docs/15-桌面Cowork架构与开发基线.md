# 15 · 桌面 Cowork 架构与开发基线

> **存储迁移已完成**（2026-08-22）：控制面在 SQLite WAL + JSONL，数据面也下了本地
> （`~/.workpilot/kb/<slug>/` 的 FAISS + BM25）。PostgreSQL / Redis / Arq / pgvector 全部退役，
> 见 [ADR-0012](adr/0012-退役postgres与redis改用本机文件.md)。**"Cowork" 现在就是整个产品**，
> 不再是与 RAG 并列的一条线：沉浸阅读并成它的一档工作模式
> （[ADR-0013](adr/0013-沉浸阅读作为工作模式而非第二条产品线.md)）。

> 状态：Cowork 工具循环、会话能力授权、桌面壳、格式 Skill + Shell、Provider/连接器、MCP/Skill、
> Scheduler/Inbox、隔离只读子 Agent、只读 git 视图、常驻审批规则、飞书消息面、
> 计划模式、任务清单、长期记忆、自唤醒、沉浸阅读工具、阅读器面板与持久批注已实现；
> frozen Python sidecar、Playwright 运行时与 Tauri 原生安装包的构建闭环，以及
> “选择本机工作文件 → Progress → Artifact 安全预览/语义 diff”的黄金流程也已验证。

## 1. 目标形态

用户在统一工作台输入目标，选择要共享的本地目录并授予只读或读写权限。Agent 在后台持续
规划和调用工具，前端实时展示 Progress、文件变更与 Artifacts。系统选择器可点名本轮主要
工作文件；读写目录授权成功后，目录内
Markdown、`.docx`、`.xlsx` 可直接修改，不再对每个段落或单元格弹确认。

```text
Tauri desktop
  ├─ Next.js workbench (Chat / Progress / Artifacts / Access)
  └─ Python sidecar @ 127.0.0.1:random
       ├─ FastAPI + launch-token middleware
       ├─ cowork runs（office / reading 两档工作模式）
       ├─ tool registry + capability policy
       ├─ 本地 KB / 沉浸阅读 / memory / evidence
       └─ format Skills + persistent Shell + Artifact discovery
              │
              ├─ ~/.workpilot/cowork.db     runs, grants, artifacts, audit
              ├─ ~/.workpilot/conversations/ 规范消息 JSONL
              ├─ ~/.workpilot/kb/<slug>/     FAISS + BM25 索引
              └─ user-granted session roots: real files
```

## 2. 已落地的第一批契约

- `agent_runs.workflow_type` 接受 `cowork`，继续复用 run events、预算、checkpoint 和幂等边界。
- `session_roots` 按 owner conversation 保存规范化目录和 `read_only/read_write` 模式。
- `capability_grants` 独立表达文件读写、Shell 和外部操作权限。
- 创建读写 root 时只派生 `filesystem.read/write`；Shell/网络/外部能力不继承。
- `artifacts` 按 conversation/run 索引交付物，文件内容仍保存在用户目录。
- 创建 run 时的 `workspace_files` 只接受绝对路径；每项必须在已授权 root 内且仍是普通文件，
  规范路径会固化进 checkpoint。它是“本轮主要输入/编辑目标”，不是扫描同目录所有文件的许可。
- 桌面模式支持每次启动令牌；未携带令牌的 localhost HTTP 请求统一拒绝。
- capability 引擎对每个目标重新做 realpath/containment 检查，拒绝 `..` 和符号链接越界。
- 自研确定性 `cowork.v2` runner 已实现 provider 原生 tool-calling、canonical
  `assistant.tool_calls → tool(tool_call_id)` 历史、checkpoint、run budget、
  工具事件、失败回传、worker 心跳与失联重新入队；恢复时可升级仍在执行的 v1 checkpoint。
- Cowork 会在首选模型输入预算的 85% 触发 outbound-only compaction：canonical `messages`
  永不裁剪，checkpoint 只额外保存滚动摘要、完整工具轮次边界和 outbound tool-result 上限。
  Provider 返回实际超窗错误时，会压缩后受次数上限和 token 递减保护重试。
- Tool registry 已登记通用文件列举/读写/搜索、本地 PDF、公开网页/远程 PDF、
  Artifact 生成、格式 Skill、运行中交互与受控 `run_shell`，并为每个工具声明
  capability、risk、effect 和 parallel-safe 属性。
- 主 Cowork 首轮只下发基础工具和 WorkMode 热路径 schema；长尾能力以稳定的
  `extended_tools` 名称/摘要清单注入，需要时按准确名称调用 `load_tools`。计划模式与只读
  子 Agent 仍按副作用收窄工具面，capability、审批和路径边界仍在执行入口强制检查。
- Provider profile 支持 OpenAI、Anthropic、Gemini、DeepSeek、Qwen、Ollama 和兼容端点；
  API key 由数据库外 0600 主密钥加密，会话可独立选择 Provider 与模型覆盖。
- GitHub、飞书、企业微信、微信公众号和腾讯文档连接器支持 OAuth/令牌生命周期；飞书账号
  额外暴露日历与多维表格的固定 schema 专用工具，不要求模型自己拼 API path。模型只能调用
  固定官方 API 主机且看不到 token，外部写动作逐次审批。个人微信非官方自动化不支持。
- MCP 管理支持服务 CRUD、OAuth 绑定、目录探测/固定和逐工具策略；Skill 支持人工完整生命周期。
- `browser_open/click/back/find` 提供无脚本、无登录态的受控只读浏览会话，每次导航重新执行
  DNS 钉扎与 SSRF 校验；DOCX/XLSX/PDF 原生交付物可在 Artifacts 区预览和下载。
- 只读版本视图 `git_status/git_diff/git_log` 走固定 argv，不经 `run_shell`，因此不需要 shell 授权；
  每条命令都追加 `-- <已授权目录>` pathspec，仓库根在授权目录之外时不会泄漏其余部分的差异。
- 审批分三档：计划模式（只读）· 逐次审批（默认）· 免审批（`conversations.approval_mode`，
  只能由本机所有者在会话设置里显式打开，模型没有对应工具）。常驻规则
  （`cowork_approval_rules`）提供整只工具 / 精确目标 / argv 前缀三种粒度，只能在审批卡片上
  由用户勾选产生，并且**只省掉「再问一次」，不放大 capability**。
- 仓库可以在 `.workpilot/config.toml` 的 `[shell].allow` 里声明命令前缀，但只有在用户信任过
  那个规范化路径（`cowork_workspace_trust`）之后才生效。
- 消息面按方向拆成四块：`messaging/routing`（出站，命名 Inbox + 绑定）、
  `messaging/subscriptions`（入站，频道订阅）、`messaging/mentions`（入站，@提及拥有 thread）、
  `messaging/unrouted`（死信）。传输由 `sender` 注入，目前只有 `messaging/feishu` 一个适配器；
  事件回调没配 `encrypt_key` 就整个关闭，配了就逐条验签。
- `run_shell` 需要独立 `shell.execute` grant。无 shell 操作符且 argv 精确前缀命中部署
  allowlist 的命令可直接执行；其余命令逐次进入 Inbox 审批。执行不使用 shell 字符串拼接
  （审批过的操作符命令除外），只继承最小环境，输出有上限，cancel/timeout 会终止进程组。
  `persistent_session=true` 时命令进入会话级 PTY，活进程内保留 cwd、export、venv 与 shell
  函数；每次完成都把观测到的 cwd 写入 0600 JSON。WorkPilot 或 PTY 重启后从最后 cwd 重建，
  返回 `environment_status=lost_on_recovery`，明确要求重新准备环境而不伪装成完整恢复。
  Shell 与文件写入一样在执行前取得 `tool_invocations` 租约；执行器按 call id 二次核验审批，
  worker 在命令完成后、checkpoint 前崩溃时复用已落库结果而不重放命令。
  前台命令完成后会对授权 root 做有界差分；新建或修改的 DOCX/XLSX/PPTX/PDF 与可信
  文本格式经格式重开、大小/解压上限和 SHA-256 校验后自动登记 Artifact。扫描或登记失败
  只返回警告，不重放已经执行的命令。
  登记时同时冻结有界文本 diff；Office/PDF 先抽取段落、单元格与公式、幻灯片文字或页文本，
  不展示无意义的二进制差异。快照只用于事后审阅，不是并发写冲突门禁；单文件超过 2 MiB、
  无执行前基线或解析失败时显式返回 unavailable。
- `read_file` 自动识别 UTF-8 文本与 PDF，文本读取有字节/行数上限并返回 SHA-256；
  `write_file` 用 `purpose=workspace|artifact` 区分普通工作文件和登记交付物，覆盖既有文件时
  强制校验该 SHA，原子替换并保留有界备份。旧入口仅为 checkpoint/cassette 回放保留，不向
  新模型暴露。
- `search_files` 只搜索授权 root 中的文件名与 UTF-8 文本，跳过隐藏、依赖、
  备份目录、二进制文件和符号链接，扫描数、单文件大小与结果数均有上限。
- `fetch_url` 要求会话级 `network.read` 授权，仅接受 HTTP(S)，每次重定向
  都重新解析并拒绝本机、私有、链路本地与保留地址，响应大小和重定向数有上限。
- Office 文件默认由 `docx/xlsx/pptx/pdf` Skill 指导脚本生成新文件；明确覆盖时要求恢复副本、
  同目录临时写入、格式重开后原子替换。工具层不再维护段落/单元格白名单。
- `cowork_schedules` 持久化单次和五段 cron 计划。worker 启动时对错过的计划最多补跑一次，
  周期计划直接推进到当前时间之后的下一个触发点；同一会话存在 queued/executing/
  waiting_human run 时跳过本轮；「DB 已创建、进程内队列首次入队失败」的窗口由 tick 按持久化 `queued` 状态补偿。
- 计划创建的 run 标记为 `unattended`。它们发出的提问、目录/能力申请和 Shell 审批仍复用
  原有 Inbox 与 resume token，只增加跨会话聚合视图；无人值守不授予 standing approval，
  不改变 capability 与副作用工具的执行闸门。

对应 API：

| API | 用途 |
|---|---|
| `GET/POST /api/v1/cowork/sessions/{id}/roots` | 查看或授予会话目录 |
| `DELETE /api/v1/cowork/sessions/{id}/roots/{root_id}` | 撤销目录及其派生能力 |
| `GET/POST /api/v1/cowork/sessions/{id}/grants` | 查看或显式授予能力 |
| `DELETE /api/v1/cowork/sessions/{id}/grants/{grant_id}` | 独立撤销能力 |
| `GET /api/v1/cowork/sessions/{id}/artifacts` | 获取交付区索引 |
| `GET /api/v1/cowork/artifacts/{artifact_id}/preview` | 获取 CSP/iframe 沙箱约束的安全预览 |
| `GET /api/v1/cowork/artifacts/{artifact_id}/diff` | 获取登记时冻结的有界文本或语义 diff |
| `POST /api/v1/runs/cowork` | 初始化 checkpoint 并把动态工具任务投入 worker |
| `POST /api/v1/runs/{id}/steering` | 在当前工具批次后的安全边界注入新用户指令 |
| `POST /api/v1/runs/{id}/interactions/{token}/respond` | 回答问题或处理目录、能力、命令审批 |
| `GET/POST /api/v1/automations` | 列出或创建 Cowork 自动化计划 |
| `PATCH/DELETE /api/v1/automations/{id}` | 暂停、启用、修改或删除计划 |
| `POST /api/v1/automations/{id}/run` | 在重叠保护下立即运行计划 |
| `GET /api/v1/automations/inbox/items` | 聚合无人值守 run 等待处理的请求 |

## 3. 运行时落地顺序

### A. 桌面启动闭环（已完成）

Tauri 选择空闲端口、生成随机 launch token，开发态启动虚拟环境中的 Python，发布态只启动
安装包同目录的固定 `workpilot-sidecar`。构建脚本用 PyInstaller 冻结后端并先跑迁移/API 烟测，
Tauri `externalBin` 再把它与 headless Chromium/FFmpeg 一起装入原生包；应用关闭时回收子进程。
自动升级、正式代码签名和 macOS 公证仍属于发布工程，不由本地 bundle 成功替代。

### B. 单 Agent 通用工具循环（首版已完成）

模型通过 gateway/provider 的原生 `tools` 参数输出 tool call，不再把 action JSON 塞进文本。
运行时将工具描述、JSON Schema、风险等级、所需 capability、只读/写入属性统一登记；模型
返回的 assistant `tool_calls` 与每条带 `tool_call_id` 的 tool result 原样进入 canonical 历史。
同一批调用只有全部为 `parallel_safe` 只读工具时才使用独立数据库 session 并行，混合批次和
任何写调用都串行。每次调用产生 `tool.start/tool.result/tool.error` 事件，并沿用
`tool_invocations` 幂等租约。

上下文压缩只改变下一次 provider 请求的视图。原始用户目标始终保留；较早的完整
`assistant.tool_calls + tool results` 才能跨过摘要边界，绝不制造孤立 tool call。摘要失败会
重试一次，仍失败则在后台任务中使用确定性事实摘录；provider 的 400 超窗响应会进入同一
恢复通道，但只有 outbound token 数确实下降才允许重试，且最多重试配置的次数。每次边界
推进记录 `context.compacted` 事件，canonical checkpoint 可继续用于审计、恢复和效果评测。

当前工具集已覆盖通用 UTF-8 文件读写、目录列举、文本搜索、本地 PDF、公开网页/远程
PDF、Artifact 生成、格式 Skill 与受控 Shell。写操作串行，多只读工具可在同一模型轮次并行执行。
Shell 仅开放受 capability、argv allowlist、
逐命令审批、超时和进程组取消共同约束的 `run_shell`，不提供无边界的 Full Access 模式。
短命令可以选择会话级持久 PTY；进程内保留 cwd/env，重启只恢复最后 cwd 并显式报告 env 丢失。

### C. Office 文件能力（格式 Skill + Shell）

2026-08-22 删除独立 `/workspace` 页面、`/api/v1/editor`、`local_office_write` 与 Office
格式专用工具。Cowork 先加载 `docx/xlsx/pptx/pdf` Skill，再用通用文件工具准备脚本并在已授权
工作区以前台 `run_shell` 执行；命令后的产物差分通过格式验证后自动进入 Artifact 区。桌面输入区
把“只读资料副本”和“本机工作文件”分成两个入口：后者明示会授予所在文件夹读写，并把用户点名
文件写入 run checkpoint；后端在创建任务前再次按 capability 复核，不信任客户端传入路径。
选择本机持久 PTY 是因为桌面任务需要连续使用 cwd、venv、字体、模板和企业 CLI；权限边界仍是
session root、`shell.execute`、命令审批、租约和审计，不提供 Full Access。详见
[ADR-0016](adr/0016-格式Skill持久Shell与工作区产物.md)。

### D. 前端四区

- Chat：输入目标；只读资料上传副本，本机工作文件由系统选择器点名原件。
- Progress：按 run event 展示计划、工具、重试、错误和预算。
- Artifacts：固定右栏列出生成/修改文件，提供安全预览、变更摘要、`+/-` 统计和语义 diff。
- Access：展示每个 root 和 capability，支持升级、降级和撤销。

## 4. Scheduler 与 Unattended Inbox（首版已完成）

Scheduler 不依赖前端页面存活。嵌入式 worker 周期扫描 `next_run_at`，用
`dispatch_lease_owner` / `dispatch_lease_until` 的条件 UPDATE 抢占到期计划（SQLite 没有
`SKIP LOCKED`，但"条件 UPDATE 命中零行即抢占失败"是同一个语义，而且不需要行锁）；
每次触发创建全新的 Cowork run 和 checkpoint，
不会复用上一轮模型历史。单次计划触发或因重叠跳过后自动停用；周期计划不会逐个重放离线
期间所有时间点，而是只补一次并计算当前时间之后的下一轮。

Unattended Inbox 是人工决策的聚合读模型，不是更高权限的执行模式。运行遇到原有 HITL
边界时仍写入同一条 `cowork_inbox_items` 记录并进入 `waiting_human`；全局收件箱按 owner
身份过滤，答复仍通过既有 first-responder-wins 的 resume token 原子更新。目录失效、能力过期、
非 allowlist Shell 或外部副作用不会因为任务来自 Scheduler 而自动放行。

## 5. 只读子 Agent 与后续多 Agent 条件

当前已提供 `explore`：独立消息上下文、共享当前 run 预算，最多四轮/八次调用；它只拿到
`effect=none + risk=read` 且非 `external.action` 的工具，不能执行 Shell、写文件或请求审批。
调查轮与收尾轮在 `config/routing.yaml` 里分开登记（收尾轮不带工具，走 light）；每一轮都发
`subagent.progress` 事件，带自己那份 token 账；轮次与每次工具调用之前都看一眼取消旗，
按停止之后不会再多花一次模型调用。
可写 supervisor/format specialist 只有下列条件均满足后才增加：

1. 单 Agent 办公任务集的成功率、写入冲突率和权限拒绝率有稳定基线。
2. 子任务可携带最小化 root/capability，而不是复制主 Agent 全部权限。
3. 子 Agent 有独立预算、事件、取消和 Artifact 来源记录。
   → **事件、取消、用量记录已具备（2026-08-23）**；欠的是"独立"那半——预算仍是主 run 的
   同一份额度，只是记账分得开，以及子 Agent 产出的 Artifact 来源还没有单独标记。
4. 同文件并发写有显式串行或合并协议。
5. 对照实验能证明多 Agent 相比单 Agent 在成功率或耗时上有净收益。
   → 仍然缺：任务集里一条 `explore` 用例都没有，这条现在既证不了也证伪不了。

## 6. 近期验收标准

- 未授权目录、只读目录 Shell、符号链接逃逸和无 `shell.execute` 全部 fail closed。
- 撤销 root 后派生 capability 立即失效；权限有效时不出现逐操作确认。
- 桌面模式无 launch token 无法访问包括健康检查在内的任何 HTTP 路由。
- `answer`、`literature_review` 与 Cowork 非格式任务回归测试不受影响。
- 格式任务默认保护源文件；覆盖任务按 Skill 执行备份、临时写入、重开验证，并产生可审计
  Shell 事件与 Artifact。格式工具层不再承诺自动 baseline 冲突检测。
- 压缩前后 canonical 消息逐字节不变；摘要失败、provider 超窗和错误窗口配置都不会形成
  无界重试，已完成的写入仍保留。
- 到期计划并发扫描只派发一次；离线期间的 cron 不形成补跑风暴；同一会话不会重叠执行，
  等待人工决定的任务可从全局 Inbox 安全恢复且不会获得额外 capability。
