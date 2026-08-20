# 16 · OpenWorker P0/P1 能力对齐

> 状态：2026-08-19。本文只记录已经进入代码、客户端和测试的能力；明确的安全边界不包装成缺陷。

| 能力面 | P0/P1 实现 | 关键边界 |
|---|---|---|
| Provider 与密钥 | OpenAI、Anthropic、Gemini、DeepSeek、Qwen、Ollama、OpenAI-compatible；会话级 Provider/模型切换；Fernet 密文和数据库外 0600 主密钥 | Anthropic/Gemini profile 只接管对话，资料库 embedding 继续走系统路由 |
| 连接器与 OAuth | GitHub、飞书、企业微信、微信公众号、腾讯文档账户 CRUD、OAuth state、加密 token；固定官方主机的读/写 API 工具 | 不支持个人微信模拟登录；外部写动作逐次审批 |
| MCP client 管理 | stdio/Streamable HTTP、服务 CRUD、OAuth 绑定、探测、目录哈希固定、逐工具数据域与副作用策略 | 未策展、目录漂移、`data_scope=deny` 均不可见；stdio 需显式信任 |
| Skills 生命周期 | `SKILL.md` 安装、更新、启停、删除、ZIP 安全导入、资源读取、快照哈希和按需加载 | 自动蒸馏/评测晋升仍属于技能自进化后续阶段，不与人工生命周期混称 |
| Web 与浏览器 | 公网搜索、网页/远程 PDF 读取；`network.read` + 会话级 `browser.control` 双授权下的受控浏览器导航、编号控件点击/输入/选择、后退、页内查找、下载与截图 | 浏览器无持久 Cookie/登录态；每跳重新做 SSRF/DNS 钉扎；session 绑定会话，空闲 TTL 顺延但绝对 TTL 不顺延；动作必须逐个调用，本地文件上传仍逐次审批，下载/截图另需目录写授权 |
| 原生交付物 | 原子生成 DOCX/XLSX/PDF，覆盖需 baseline，保留有界备份；Artifacts 内联 PDF、语义预览 DOCX/XLSX | 语义预览不承诺替代 Office 的像素级排版渲染 |
| Scheduler / Inbox | 单次/五段 cron、离线最多补跑一次、重叠保护、Redis 入队补偿、立即运行、暂停/恢复/删除；跨会话 Inbox | Unattended 不自动续权；提问、目录/能力、Shell 与外部动作都会安全暂停 |
| 长期记忆 | `remember` / `memory_update` / `memory_forget` / `memory_read`；global / workspace / conversation 三级作用域，软删除，每轮钉进 system prompt；客户端记忆面板与内联撤销 | 不复用 RAG 的 memory：那套做时序有效性建模（ADR-0005），且 `rag ⊥ cowork`；只注入本会话可见的那部分，别的会话与目录的记忆一律不可见 |
| 计划模式 | 会话发起时可选；计划阶段只下发只读与交互工具，`propose_plan` 提交方案后暂停，批准即翻转运行时模式并把步骤变成任务清单；客户端计划卡片支持批准或带修改意见退回 | 批准是 checkpoint 里 `mode` 的翻转，不是 prompt 约定：未批准前写工具既不下发，执行边界也拒绝；准入判据是 `risk`/`execution` 而不是工具名单 |
| 任务清单 | `todo_write` 整份替换的 pending/in_progress/done 清单，存进 checkpoint、钉进每轮 system prompt、前端独立渲染 | 与 `agent_plan_steps` 并存不互相替代：前者是模型主动声明的计划，后者是 runtime 从 tool call 派生的事后日志 |
| 只读子 Agent | `explore` 独立上下文、共享预算、轮次/调用上限、证据工具记录 | 过滤所有副作用、Shell 与 `external.action`，当前不开放可写子 Agent |
| 工具规模治理 | 核心目录、按目标相关选择、`search_tool_catalog` 动态激活；历史 tool_call 引用过的 schema 跨话题保留 | 目录按轮重算且有上限，不因某个工具曾被下发过就永久驻留；只有历史调用过的和显式激活的是单调的 |

统一安全不变量分两层。**注册表入口**（`CoworkToolRegistry.execute`）校验 capability 与
`extra_capabilities`、一次性 call-id 审批、裁剪目录的 `allowed` 白名单，并在副作用发生前取得
`tool_invocations` 租约——调用方、恢复路径、Scheduler 与子 Agent 都走这里，没有旁路。
**主循环编排层**（`runtime.decide`）额外强制"独占工具必须单独调用"；这是一批调用之间的
约束，单次执行入口在语义上无法校验它。
