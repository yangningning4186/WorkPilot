# ADR-0012 退役 PostgreSQL 与 Redis，本机文件即事实

**状态**：已采纳
**日期**：2026-08-22
**取代**：[ADR-0002](0002-pgvector-而非专用向量库.md)

## 背景与约束

产品形态在 2026-08 变了。立项时它是一个跑在开发机上、要往公网发 demo 的 Web 服务，
所以按 Web 服务的常识选了基础设施：PostgreSQL 存一切、Redis 做缓存与队列、Arq 派任务。

真正在用的是**桌面 Cowork**（[ADR-0009](0009-桌面sidecar与会话级能力授权.md)）：一个
Tauri 壳 + localhost sidecar，读用户自己硬盘上的文件，全部计算发生在本机。在这个形态下
原来的选型变成了三条纯负债：

1. **两个容器只为一个用户服务。** 桌面应用不能要求用户先装 OrbStack 再 `docker compose up`。
2. **双轨代码。** `COWORK_STORE_BACKEND` 让 24 个文件里每个存储函数都写了两遍，
   两条分支的行为悄悄分叉——`list_cowork_office_files` 在 SQLite 模式下查的是一张永远空的
   Postgres 表，Office 文件列表因此一直是空的，直到删分支时才被发现。
3. **事务保证换来的东西，这个形态用不上。** ADR-0002 拿 chunk 与业务表同库、可联表、
   可事务当作 pgvector 的主要理由。桌面端的知识库是用户指着一个文件夹说"索引这个"，
   索引是派生数据，删了重建是秒级——没有需要跨表事务保护的一致性。

## 决策

**全部状态落到本机文件，进程内完成调度。** 具体：

| 原来 | 现在 | 位置 |
|---|---|---|
| PostgreSQL：run / 事件 / checkpoint / 幂等 / 授权 / 记忆 | SQLite | `~/.workpilot/cowork.db` |
| PostgreSQL：conversations / messages | append-only JSONL + SQLite 索引 | `~/.workpilot/conversations/` |
| PostgreSQL：llm_calls / 预算闸门 | SQLite（钱存整数微美元） | `~/.workpilot/telemetry.db` |
| pgvector + PG 全文索引 | FAISS + BM25，一个 KB 一个目录 | `~/.workpilot/kb/<slug>/` |
| PostgreSQL：provider / connector 账户 | 0600 JSON | `~/.workpilot/*.json` |
| Redis：补全缓存 | 进程内 LRU（512 条） | `workpilot_ai/cache.py` |
| Redis：编辑器限时授权 | 进程内 dict（sha256(token) → 到期时刻） | `rag/editor_permissions.py` |
| Redis Streams：run 事件总线 | 进程内 `InMemoryRunBus` | `core/run_bus.py` |
| Arq：任务队列 | 进程内队列 + 嵌入式 worker | `core/queue.py` |
| Alembic 迁移 | `PRAGMA user_version` + `ALTER TABLE ADD COLUMN` | `cowork_store/sqlite.py` |

`deploy/docker-compose.yml` 现在是 `services: {}`。开发环境的启动步骤是 `uv run uvicorn`，
没有别的。

## 考虑过的替代方案

| 方案 | 优点 | 放弃理由 |
|---|---|---|
| **保留 Postgres，只把桌面端做成第二后端** | 评测那条路继续享受事务与联表 | 这正是被删掉的现状。双轨的成本不在写，在**验**：每加一个存储函数就要在两个后端各测一遍，而实际上只有一条路被人用着，另一条静静地烂掉 |
| **换 SQLite 但保留 Redis 做队列** | 队列语义现成，重启不丢任务 | 单用户单进程，队列的唯一消费者就在同一个进程里。持久化真相本来就在 SQLite 的 `queued` 状态上，队列只负责降低唤醒延迟——那用 `asyncio.Queue` 就够了 |
| **换 SQLite 但保留 pgvector 做评测** | 评测口径不动 | 向量召回在合并记忆时已经没有调用者了；KB 索引又是可重建的派生数据。为一条没人调的路径留一个容器 |
| **DuckDB / LanceDB 替代 SQLite + FAISS** | 单文件、列存、向量原生 | SQLite 在 Python 标准库里，零依赖零版本风险；FAISS 是 LlamaIndex 的默认后端，换一个要自己写适配。收益是性能，而这个规模上性能不是问题 |

## 接受的代价

1. **失去跨表事务，但没有失去候选发布边界。** 约束 10 在 KB 路径上改成文件系统协议：
   候选先完整写入独立的 `versions/<id>/`，成功后才原子更新 manifest；失败的候选不可见，
   已有版本和 active 都不动。激活不再与 `chunks.is_searchable` 投影同事务，而是一个明确的
   `active_version` 指针；指针损坏时检索拒绝，不猜测回落。详见
   [ADR-0014](0014-知识库索引版本化与显式激活.md)。
2. **embedding 空间保护从“库级拒绝”变成“版本级拒绝”。** 每个版本各自记录签名
   （model + dimensions + revision）与检索配置。换模型后可以另建一版并与旧版共存，但用
   当前模型检索一个签名不兼容的版本仍会明确失败。代价是版本越多占用的磁盘越多。
3. **失去多进程并发写。** SQLite 的写是全库串行的。单用户桌面产品上这不是约束，但它
   堵死了"起两个 worker"这条路。原来靠 `SELECT ... FOR UPDATE` 做的四处行锁改成了
   `local_run_guard(run_id)` 的分片 asyncio 锁——**同一进程内**有效，跨进程无效。
4. **`claim_run` 语义收紧。** Postgres 版允许第二个 worker 直接偷走过期租约；SQLite 版
   只认 `queued`。那条偷租约的路绕开了 `recovery_count`，而
   [ADR-0007](0007-agent幂等与事件溯源.md) 正是用它挡住"稳定把 worker 拖垮的 run 被无限
   重投"。现在回收必须经过 watchdog，重投次数因此必然被计数。
5. **进程重启丢掉进程内状态**：补全缓存、编辑器授权、KB 建索引作业的进度、OAuth 待授权
   flow。前三者重建代价极低；OAuth flow 过期后用户重新点一次即可。**唯一真正要紧的
   是不能丢的东西一件都没进内存**——run、事件、checkpoint、幂等租约全在 SQLite 里。

## 重新审视的触发条件

- 需要多用户或需要把服务放到公网长期运行（那时 §3 的单进程假设直接失效）
- 单个 KB 超过 100 万 chunk，FAISS 平坦索引的内存或延迟成为瓶颈
- 出现真正需要跨"知识库"与"run"两个域做事务的功能

## 后续影响

- 约束 10（候选版本全部成功才激活）在 KB 路径上由“独立版本目录先构建、manifest 后发布”
  承担；embedding 签名负责阻止跨空间误检索。两者缺一不可，见
  [04 §3](../04-知识与阅读设计.md)。
- `app/core/db.py` 保留为**惰性对象**而不是删除：`session` 形参还穿在约 190 处签名上，
  一次性摘掉是纯机械大改。现在 `DbSession.execute()` 直接抛错，谁再偷偷写一条 SQL 会在
  第一次运行时当场炸出来，而不是安静地把 Postgres 依赖带回来。签名清理是后续一次独立改动。
- `/health/ready` 探的是"本机 SQLite 能不能打开"，那是唯一剩下的外部依赖。
- `eval/cowork_runner.py` 曾要求 `COWORK_STORE_BACKEND=postgres`，**2026-08-22 已收编**：
  那个开关本身也一并删掉了。留着它比删掉更危险——唯一的读者是启动时那句
  `if backend == "sqlite"`，填成 "postgres" 不报错，只是静默跳过本地 store 初始化，
  然后每一次请求都撞「本地 store 尚未初始化」。跑批需要的隔离改由 `cowork_data_path`
  承担：控制面指到 `<package>/store/`，见 [06 §4.5](../06-评测体系.md)。
