# ADR-0009 桌面 sidecar 与会话级能力授权

**状态**：已采纳；Office 专用能力条款被 ADR-0016 取代，宽泛 capability 条款被 ADR-0017 取代
**日期**：2026-08-18

## 背景与约束

WorkPilot 要从网页知识助手演进为本地办公 Cowork：用户在前端输入目标，系统可以读取
用户选定的目录，并在得到一次权限后直接修改 Markdown、Word 和 Excel。目标体验参考
OpenWorker，但必须保留 WorkPilot 已有的 RAG 溯源、run 事件恢复和 Office 精确执行器。

这里有四个硬约束：

1. 浏览器页面不能天然获得任意文件系统权限；本地能力必须由桌面进程承载。
2. “授权后直接操作”不能退化为每次工具调用弹确认，也不能等同于全盘 Full Access。
3. 目录读写授权不得隐式扩大为 Shell 执行、网络发送或外部系统写入。
4. 已有 `answer` 与 `literature_review` 工作流必须继续运行，不能为 Cowork 一次性重写。

## 决策

采用 **Tauri 桌面壳 + 绑定 `127.0.0.1` 的 Python sidecar + 现有 Next.js UI**。桌面父进程
每次启动生成至少 256 bit 随机令牌，仅通过子进程环境和前端内存传递；sidecar 对所有 HTTP
请求校验 `X-WorkPilot-Launch-Token`，生命周期跟随桌面父进程。

Cowork 复用 `agent_runs`、`run_events`、checkpoint 与工具幂等表，新增第三种
`workflow_type=cowork`，而不是建立第二套任务系统。第一阶段仍是一个通用 Agent 加受限工具
循环；多 Agent specialist 要等单 Agent 的权限、预算、Artifact 和评测闭环稳定后再引入。

权限采用会话级 capability grant：

- 用户通过系统目录选择器授予 `read_only` 或 `read_write` root；后端保存规范化绝对路径。
- `read_only` 自动获得 `filesystem.read`。
- `read_write` 自动获得 `filesystem.read`、`filesystem.write`。2026-08-22 起 Office 文件
  使用独立 `shell.execute` + 格式 Skill；不再派生格式专用 capability。
- `network.read`、`shell.execute` 与 `external.action` 必须独立授权，
  永远不从目录权限继承。网页读取不借用语义过宽的“外部写操作”授权。
- 每次工具执行仍重新解析真实路径、检查符号链接逃逸、root 状态、grant 撤销与过期状态。
- 多个 root/grant 采用**加法语义**：嵌套的 `read_only` root 不会从父级 `read_write` root
  扣除权限。需要排除子目录时必须拆分父级授权范围或撤销父 root，不能把只读 root 当 deny rule。

产物统一登记到 `artifacts`，实际文件继续留在用户目录。Artifacts 是 Agent 执行结果和前端
交付区之间的稳定边界，不把数据库变成用户文件的第二份真相源。

## 考虑过的替代方案

| 方案 | 优点 | 放弃理由 |
|---|---|---|
| 纯 Web + 浏览器 File System Access API | 开发量小 | 浏览器与平台支持不一致，后台任务和重启恢复弱，无法可靠承载本地 sidecar 工具 |
| 后端默认拥有用户主目录 | 交互最少 | 权限范围不可见且过宽，一次 prompt/tool 漏洞即可访问无关文件 |
| 每次写操作 HITL 确认 | 风险直观 | 与用户明确要求冲突，批量办公任务被大量确认打断 |
| 单一 `full_access` 开关 | 模型与 UI 简单 | 文件授权会连带解锁 Shell/外部写操作，无法最小授权和独立撤销 |
| 先做多 Agent supervisor | 展示效果强 | 在工具权限、任务恢复和结果合并尚未稳定时增加不可观测的并发与责任边界 |
| 单独重建 Cowork 任务表 | 与旧逻辑隔离 | 产生两套事件流、恢复、预算和前端时间线，长期维护成本更高 |

## 接受的代价

- Tauri、sidecar 打包、升级和跨平台签名形成新的发布链路。
- capability 检查必须位于每个副作用工具的统一入口；漏接一个工具就会形成绕过路径。
- 按会话保存 root 会产生授权管理 UI 和陈旧目录清理工作。
- Office 专用能力已退役；格式正确性由 Skill、脚本验证与 Artifact 扫描共同承担。
- 第一阶段不会宣称多 Agent；短期展示重点是可恢复执行、进度和可交付文件。

## 后续影响

1. Tauri 不得把 sidecar 固定在公开网卡，也不得把 launch token 写入磁盘或日志。
2. Tool registry 必须声明所需 capability、风险等级、是否只读和是否可并行。
3. 安全读工具可并行；任何 `effect != none` 的工具都必须走 `tool_invocations` 幂等租约，
   不得只按 `risk=write` 判断。Shell 执行器还必须按当前 `tool_call_id` 复核 allowlist 或一次性审批。
   同一文件必须有冲突摘要与幂等键。
4. `local_office_write`、独立 `/workspace` 页面与 Office 专用工具均已于 2026-08-22 删除；
   当前边界见 [ADR-0016](0016-格式Skill持久Shell与工作区产物.md)。
5. 多 Agent 前必须补齐委派事件、子 Agent 独立预算/capability、Artifact 合并和并发冲突评测。
