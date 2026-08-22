# 15 · 桌面 Cowork 架构与开发基线

> **存储迁移已完成**（2026-08-22）：控制面在 SQLite WAL + JSONL，数据面也下了本地
> （`~/.workpilot/kb/<slug>/` 的 FAISS + BM25）。PostgreSQL / Redis / Arq / pgvector 全部退役，
> 见 [ADR-0012](adr/0012-退役postgres与redis改用本机文件.md)。**"Cowork" 现在就是整个产品**，
> 不再是与 RAG 并列的一条线：沉浸阅读并成它的一档工作模式
> （[ADR-0013](adr/0013-沉浸阅读作为工作模式而非第二条产品线.md)）。

> 状态：Cowork 工具循环、会话能力授权、桌面壳、Office、Provider/连接器、MCP/Skill、
> Scheduler/Inbox、隔离只读子 Agent、只读 git 视图、常驻审批规则、飞书消息面、
> 计划模式、任务清单、长期记忆、自唤醒与沉浸阅读工具已实现。
> **尚未落地：阅读器面板前端**——`reader_goto` 已经在发翻页指令，但没有地方显示。

## 1. 目标形态

用户在统一工作台输入目标，选择要共享的本地目录并授予只读或读写权限。Agent 在后台持续
规划和调用工具，前端实时展示 Progress、文件变更与 Artifacts。读写目录授权成功后，目录内
Markdown、`.docx`、`.xlsx` 可直接修改，不再对每个段落或单元格弹确认。

```text
Tauri desktop
  ├─ Next.js workbench (Chat / Progress / Artifacts / Access)
  └─ Python sidecar @ 127.0.0.1:random
       ├─ FastAPI + launch-token middleware
       ├─ cowork runs（office / reading 两档工作模式）
       ├─ tool registry + capability policy
       ├─ 本地 KB / 沉浸阅读 / memory / evidence
       └─ Markdown / Word / Excel executors
              │
              ├─ ~/.workpilot/cowork.db     runs, grants, artifacts, audit
              ├─ ~/.workpilot/conversations/ 规范消息 JSONL
              ├─ ~/.workpilot/kb/<slug>/     FAISS + BM25 索引
              └─ user-granted session roots: real files
```

## 2. 已落地的第一批契约

- `agent_runs.workflow_type` 接受 `cowork`，继续复用 run events、预算、checkpoint 和幂等边界。
- `session_roots` 按 owner conversation 保存规范化目录和 `read_only/read_write` 模式。
- `capability_grants` 独立表达文件读写、Word/Excel 编辑、Shell 和外部操作权限。
- 创建读写 root 时一次性派生文件与 Office 能力；Shell/外部能力不继承。
- `artifacts` 按 conversation/run 索引交付物，文件内容仍保存在用户目录。
- 桌面模式支持每次启动令牌；未携带令牌的 localhost HTTP 请求统一拒绝。
- capability 引擎对每个目标重新做 realpath/containment 检查，拒绝 `..` 和符号链接越界。
- LangGraph `cowork.v2` runner 已实现 provider 原生 tool-calling、canonical
  `assistant.tool_calls → tool(tool_call_id)` 历史、checkpoint、run budget、
  工具事件、失败回传、worker 心跳与失联重新入队；恢复时可升级仍在执行的 v1 checkpoint。
- Cowork 会在首选模型输入预算的 85% 触发 outbound-only compaction：canonical `messages`
  永不裁剪，checkpoint 只额外保存滚动摘要、完整工具轮次边界和 outbound tool-result 上限。
  Provider 返回实际超窗错误时，会压缩后受次数上限和 token 递减保护重试。
- Tool registry 已登记通用文件列举/读写/搜索、本地 PDF、公开网页/远程 PDF、
  Artifact 生成、Office 读写、运行中交互与受控 `run_shell`，并为每个工具声明
  capability、risk、effect 和 parallel-safe 属性。
- 工具目录采用“核心工具 + 目标相关工具 + `search_tool_catalog` 动态激活”，避免 Provider、
  MCP 和连接器增长后把完整 schema 一次性塞满上下文；被激活工具仍走同一注册表与授权入口。
- Provider profile 支持 OpenAI、Anthropic、Gemini、DeepSeek、Qwen、Ollama 和兼容端点；
  API key 由数据库外 0600 主密钥加密，会话可独立选择 Provider 与模型覆盖。
- GitHub、飞书、企业微信、微信公众号和腾讯文档连接器支持 OAuth/令牌生命周期；模型只能
  调用固定官方 API 主机且看不到 token，外部写动作逐次审批。个人微信非官方自动化不支持。
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
  Shell 与文件写入一样在执行前取得 `tool_invocations` 租约；执行器按 call id 二次核验审批，
  worker 在命令完成后、checkpoint 前崩溃时复用已落库结果而不重放命令。
- Word/Excel Cowork 入口会在执行器内部再次校验会话 root capability；写工具在副作用前
  抢占 `tool_invocations` 幂等租约，成功后自动登记 Artifact。
- `read_text_file` 有字节/行数上限并返回 SHA-256；`write_text_file` 与
  `create_artifact` 覆盖既有文件时强制校验该 SHA，原子替换并保留有界备份。
- `search_files` 只搜索授权 root 中的文件名与 UTF-8 文本，跳过隐藏、依赖、
  备份目录、二进制文件和符号链接，扫描数、单文件大小与结果数均有上限。
- `fetch_url` 要求会话级 `network.read` 授权，仅接受 HTTP(S)，每次重定向
  都重新解析并拒绝本机、私有、链路本地与保留地址，响应大小和重定向数有上限。
- Excel 编辑对图表、图片、透视表、图表工作表与切片器 fail closed，公式采用安全函数白名单；
  目录扫描跳过依赖、隐藏目录与备份目录，并受遍历条目上限约束。
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
| `POST /api/v1/runs/cowork` | 初始化 checkpoint 并把动态工具任务投入 worker |
| `POST /api/v1/runs/{id}/steering` | 在当前工具批次后的安全边界注入新用户指令 |
| `POST /api/v1/runs/{id}/interactions/{token}/respond` | 回答问题或处理目录、能力、命令审批 |
| `GET/POST /api/v1/automations` | 列出或创建 Cowork 自动化计划 |
| `PATCH/DELETE /api/v1/automations/{id}` | 暂停、启用、修改或删除计划 |
| `POST /api/v1/automations/{id}/run` | 在重叠保护下立即运行计划 |
| `GET /api/v1/automations/inbox/items` | 聚合无人值守 run 等待处理的请求 |

## 3. 运行时落地顺序

### A. 桌面启动闭环

建立 `desktop/` Tauri 工程；选择空闲端口，生成随机 launch token，启动 Python sidecar，
完成健康检查后加载工作台。关闭窗口、崩溃恢复和自动升级都必须正确回收 sidecar。

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
PDF、Artifact 生成及 Office 专用编辑。写操作串行，多只读工具可在同一模型轮次并行执行。
Shell 仅开放受 capability、argv allowlist、
逐命令审批、超时和进程组取消共同约束的 `run_shell`，不提供无边界的 Full Access 模式。

### C. 现有 Office 工作台迁移（Cowork 路径已完成）

当前 SHA-256 冲突检查、备份、原子替换和格式重开验证已复用到 Cowork session root。
Cowork Word/Excel 执行器入口直接调用 capability 引擎，不依赖 `local_office_write`，
目录授权后不产生逐操作确认。旧 `/workspace` 流程仍保留该限时授权作为兼容入口（载体已从
Redis 换成进程内，[ADR-0008](adr/0008-限时授权后直接编辑本地办公文件.md)）。

### D. 前端四区

- Chat：输入目标、附加目录/文件、查看最终答复。
- Progress：按 run event 展示计划、工具、重试、错误和预算。
- Artifacts：预览、打开、定位和恢复生成/修改的文件。
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
可写 supervisor/office specialist 只有下列条件均满足后才增加：

1. 单 Agent 办公任务集的成功率、写入冲突率和权限拒绝率有稳定基线。
2. 子任务可携带最小化 root/capability，而不是复制主 Agent 全部权限。
3. 子 Agent 有独立预算、事件、取消和 Artifact 来源记录。
4. 同文件并发写有显式串行或合并协议。
5. 对照实验能证明多 Agent 相比单 Agent 在成功率或耗时上有净收益。

## 6. 近期验收标准

- 未授权目录、只读目录写入、错误 Office 后缀、符号链接逃逸全部 fail closed。
- 撤销 root 后派生 capability 立即失效；权限有效时不出现逐操作确认。
- 桌面模式无 launch token 无法访问包括健康检查在内的任何 HTTP 路由。
- `answer`、`literature_review` 和现有 Office 工作台回归测试不受影响。
- 所有文件写操作继续具备备份、原子替换、格式验证、冲突检测与可审计事件。
- 压缩前后 canonical 消息逐字节不变；摘要失败、provider 超窗和错误窗口配置都不会形成
  无界重试，已完成的写入仍保留。
- 到期计划并发扫描只派发一次；离线期间的 cron 不形成补跑风暴；同一会话不会重叠执行，
  等待人工决定的任务可从全局 Inbox 安全恢复且不会获得额外 capability。
