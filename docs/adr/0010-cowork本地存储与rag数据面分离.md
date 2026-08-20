# ADR-0010：Cowork 本地存储与 RAG 数据面分离

- 状态：Accepted（桌面切换完成，服务端保留兼容后端）
- 日期：2026-08-19

## 决策

桌面 Cowork 的控制面迁移到 `SQLite WAL + JSONL + 文件系统`；论文与资料库 RAG
继续使用 PostgreSQL + pgvector。Web/集群部署保留 PostgreSQL + Redis/Arq 适配器。

| 数据 | 真相源 |
|---|---|
| run、event、checkpoint、plan、HITL、工具幂等租约、Scheduler | `cowork.db` |
| 规范 user/assistant/tool 消息 | `conversations/<id>.jsonl` |
| Skills、MCP 配置、附件、交付物、预览 | 应用数据目录文件系统 |
| documents、versions、blocks、chunks、embedding、引用与检索评测 | PostgreSQL + pgvector |

`knowledge.read` 是独立的全局授权；资料库检索不得借用 `filesystem.read`，撤销该
grant 后 `search_knowledge` 必须在进入 RAG 数据面前失败关闭。

JSONL 只承担 append-only 消息，不承担租约、审批或调度锁。SQLite 使用 WAL、
`busy_timeout` 和短 `BEGIN IMMEDIATE` 事务；任何模型、浏览器、Shell、MCP 调用期间
不得持有数据库事务。

## 队列与事件

桌面 sidecar 采用同进程 `RunQueue` / `RunBus`，四个 consumer 提供低延迟唤醒；
dispatcher 根据持久化 `queued` 状态轮询补偿，因此内存通知允许丢失。工具副作用仍以
store 中的 call-id 幂等租约为唯一防重边界。

Web/集群继续使用 Arq 与 Redis pub/sub。Redis 只负责唤醒，不是 run/event 真相源。

## RAG 边界

Cowork 只依赖 `RagService.search()` 返回的完整可溯源 `EvidenceSegment`，不得直接查询
`documents/chunks`。当前 `PostgresRagService` 是同进程适配器，后续可替换为本机 HTTP
sidecar，而不改变 Cowork store。

## 迁移顺序与兼容性

1. 引入 `CoworkStore`、`RunQueue`、`RunBus` 接口以及 SQLite/JSONL 实现。
2. 桌面先切换为进程内队列与轮询，移除 Redis/Arq 硬依赖。
3. 按 run/event → checkpoint/lease → permissions/artifacts → Scheduler/Inbox 的顺序，
   将现有 PostgreSQL service 调用切到 `CoworkStore`。
4. 消息改为 JSONL 真相、SQLite 可重建索引；迁移器按 record-id 去重。
5. 完成双读校验后才把桌面默认 `COWORK_STORE_BACKEND` 切为 `sqlite`。

`app.cli.migrate_cowork_store` 对上述控制面表和 JSONL 消息执行幂等回填，并从
PostgreSQL 与本地介质独立读取，比较逐表数量和 SHA-256 摘要。只有全部一致才写入
`sqlite-ready`。桌面 sidecar 的 migrate 阶段执行该闸门，成功后以
`COWORK_STORE_BACKEND=sqlite` 启动；服务端部署仍可显式选择 `postgres`。不得因为
SQLite schema 已存在就宣称迁移完成，也不得在同一 run 中混用两个真相源。

## 后果

- 普通桌面 Cowork 最终不再要求 Redis，也不因 RAG 数据库维护而丢失本地任务状态。
- SQLite 模式限定单机；多机器 worker 必须使用服务端适配器。
- JSONL 与 SQLite 没有跨文件事务，因此消息 record-id、fsync、可重建索引和损坏尾行
  恢复属于协议的一部分。
- 长期记忆与 Skill 候选仍落 PostgreSQL。桌面完成 Cowork 后，把本地 run/message 的
  不可变来源快照写入可靠作业表；外部 UUID 用于幂等和审计，不重新制造 PostgreSQL
  Cowork run/checkpoint 影子记录。
