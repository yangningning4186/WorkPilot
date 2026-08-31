# ADR-0018 Agent 消息队列采用持久化有序交付

**状态**：已采纳

**日期**：2026-08-29

## 背景与约束

Cowork 原先只有一条进程可见的 steering 队列。它能在模型回合之间修改当前 run，但不能表达
“本轮回答结束后继续”或“当前 run 已终止，另起一轮”。如果 follow-up 与 run 终态并发，分别
提交“终态”和“检查队列”会留下一个窗口：消息可能在检查之后入队、在终态之前落库，既不会被
当前 run 领取，也不会触发新 run。

桌面 sidecar 可能随时退出，因此队列不能依赖内存任务。与此同时，启动新 run 会跨越多次 SQLite
写入和内存唤醒，无法假设 exactly-once；只能用持久来源标识实现 effectively-once。

## 决策

采用一张持久化、有来源、FIFO 的 Agent 消息队列，并把交付意图定义为封闭类型：

| delivery | 语义 | 领取边界 |
|---|---|---|
| `steer` | 在当前活跃 run 的下一个模型回合前注入 | 回合开始 |
| `follow_up` | 当前 run 产出一次 final 后，在同一 run 继续外层循环 | final 与终态之间 |
| `next_run` | 当前 run 终止后创建一个后继 run | 终态提交之后 |

每条消息同时保存 `requested_delivery` 与 `delivery`。如果 `follow_up` 到达时 run 已终止，或其
follow-up 边界已经封闭，存储层在入队事务内把有效交付改为 `next_run`；来源 provenance 原样
续传，不能把本地 owner 的消息降成未知来源，也不能把外部消息升级为 owner 授权依据。

### 原子终态边界

当前 worker 通过 `claim_follow_up_or_seal(run_id, worker_id)` 执行一个 SQLite 写事务：

1. 有待领取的 `follow_up`：按 `(created_at, id)` 全部标记 `consumed`，run 继续；
2. 没有待领取消息：写入 `cowork_run_queue_state.follow_up_open = 0`，封闭该 run。

入队事务读取同一状态。因此 enqueue 与 seal 无论谁先取得写锁，消息都只会有两种合法结果：被
当前 run 消费，或被改投 `next_run`。不存在“pending follow-up 挂在终态 run 上”的第三种状态。

run 的终态更新与 `next_run` 队首提升在同一事务完成。每个 run 最多一条 `ready` 消息，其余保持
`pending`；消费或取消队首时再提升下一条，防止并行启动多个后继 run。

### 崩溃恢复与幂等消费

后台 dispatcher 只扫描 `ready next_run`。它用 queue message id 作为新 run 的
`source_wake_id`：

- 创建后、标记消费前崩溃：重试返回同一个 run，不重复创建；
- 标记消费后、内存 enqueue 前崩溃：持久化的 queued run 由常规 dispatcher 补偿；
- 多条 `next_run`：消费队首时将剩余尾部重绑定到新 run，等该 run 终止后再提升一个后继。

`cancel` 只允许作用于 `pending` / `ready`；已消费消息不可撤销。队首取消与下一条提升同事务。
队列的 enqueue、consume、cancel 另写类型化 session record / run event，供恢复和审计使用。

## 考虑过的替代方案

| 方案 | 优点 | 放弃理由 |
|---|---|---|
| 只保留 steering，final 后用户再手动发送 | 实现最少 | 无法表达 durable follow-up；终态竞态会丢交付意图 |
| 终态提交后再查询 follow-up | 代码直观 | 查询与终态不原子，检查之后入队的消息会成为孤儿 |
| 每条 follow-up 都立即起新 run | 生命周期简单 | 丢失同一 run 的上下文、预算和工具状态；活跃 run 时还会制造并发写冲突 |
| 所有终态消息都标为 ready | 吞吐高 | 同一会话可能并行启动多个后继，破坏消息顺序和 active-run 不变量 |
| 用内存队列加去重集合 | 延迟低 | sidecar 崩溃后意图和去重状态一起消失 |
| 完整照搬 LaneState reducer | 恢复模型最完整 | 当前只需要三种消息交付；一次引入完整操作日志会扩大迁移和验证面 |

## 接受的代价

- 消息交付多一层 `pending / ready / consumed / cancelled` 状态机和周期 dispatcher。
- 新 run 的创建与 queue message 消费不是单一数据库调用；依赖 `source_wake_id` 提供
  effectively-once，而不是宣称 exactly-once。
- `follow_up` 在边界封闭后可能表现为新 run，这是避免孤儿消息的显式语义，不保证永远复用原 run。
- 当前先扩展所需 record 类型，不建立完整 LaneState reducer；更多 harness action 仍逐步迁移。

## 后续影响

1. HTTP/UI 必须展示 requested/effective delivery 和取消结果，不能只显示一条无类型文本。
2. 所有新入口必须保存 provenance；`runtime` / `unknown` 不得触发 owner-only 语义审批。
3. 回归测试固定覆盖终态两侧、队首取消、FIFO、dispatcher 崩溃重试和重复 tick。
4. compaction、forced-final、abort 与后续手动驱动 action 复用同一 session record 账本。
5. 若未来允许同会话并行 lane，必须先把“单 ready 后继”从 conversation 级不变量提升为 lane 级不变量。
