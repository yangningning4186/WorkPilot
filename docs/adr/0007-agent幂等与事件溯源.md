# ADR-0007 Agent 幂等落在工具副作用边界，事件溯源支撑流式恢复

**状态**：已采纳
**日期**：2026-08-13
**取代**：初版 `agent_steps` 单表设计与 SSE 协议

## 背景：初版的幂等模型不成立

初版设计：

```sql
CREATE TABLE agent_steps (
    run_id, step_idx, node, tool_name, ..., retry_of,
    UNIQUE (run_id, step_idx)   -- 注释写着"幂等：断点续跑不重复执行"
);
```

三处矛盾，任何一处都足以让这个约束失效：

1. **同一计划步骤会产生多条记录**：planner、executor、reflector 三个节点都要写行，
   `UNIQUE(run_id, step_idx)` 直接冲突
2. **`retry_of` 字段自相矛盾**：既然允许重试，重试写新行时 `step_idx` 必然不同，
   唯一约束根本拦不住重复执行
3. **最要命的一条**：LangGraph 从 `interrupt()` 恢复时，
   **会从所在节点的开头重新执行**，而不是从中断的那一行继续。
   节点内 `interrupt()` 之前的所有副作用都会重跑。

第 3 条意味着：`write_note` 在人工确认前已经写了一次笔记，
用户点"确认"之后会**再写一次**。而 `write_note` 正是 HITL 保护的那个操作——
保护机制本身制造了重复写入。

## 决策

### 1. 拆成四张表，各司其职

| 表 | 职责 | 唯一约束 |
|---|---|---|
| `agent_plan_steps` | 逻辑步骤（计划的一行） | `(run_id, step_idx)` |
| `agent_attempts` | 每次尝试（同一步骤可多次） | `NULLS NOT DISTINCT (run_id, plan_step_id, attempt_no, node)` |
| `run_events` | 时间线顺序（事件溯源） | `(run_id, seq)` |
| `tool_invocations` | **副作用去重** ★ | `idempotency_key` |

初版把这四件事压在一个 `step_idx` 上，所以怎么定义都是错的。

### 2. 幂等落在工具副作用边界

```
idempotency_key = hash(run_id, plan_step_id, tool_name, canonical(args))
```

有副作用的工具执行前先抢占：

```sql
INSERT INTO tool_invocations (idempotency_key, ..., status)
VALUES ($key, ..., 'in_flight')
ON CONFLICT (idempotency_key) DO NOTHING
RETURNING idempotency_key;
```

- 抢到 → 持有可续租 lease → 执行工具 → `UPDATE status='succeeded', result=...`
- 没抢到且已 `succeeded` → **直接返回存储的 result，不重复执行**
- `failed` 或过期 `in_flight` → 用条件 UPDATE 原子换新 lease，禁止先读后写

**关键点**：`idempotency_key` 不含 `attempt_no`。
同一步骤的重放（无论来自节点重执行、进程重启、还是用户重复点确认）
算出同一个 key，因此天然去重。
真正需要重跑时（比如参数改了），args 变化 → key 变化 → 允许执行。

这只提供有明确边界的 **effectively-once**。对任意外部副作用，
数据库幂等行无法与下游操作形成原子事务；工具必须把同一幂等键传给下游，
或像 `write_note` 一样用临时文件 + fsync + 原子 rename 缩小不确定窗口。

### 3. run_events 作为流式恢复的唯一真相源

初版 SSE 协议在边界情况表里写了"事件带递增 seq，重连时带 `Last-Event-ID` 续发"，
但事件类型定义里既没有 `seq` 也没有 `run_id`，更没有可回放的持久化事件表——
**需求写了，实现无处落脚**。

```sql
CREATE TABLE run_events (run_id, seq, type, payload, created_at, PRIMARY KEY (run_id, seq));
```

配套的执行模型改为：

```
POST /runs                        创建 run，任务入 Arq 队列
Arq worker                        独立执行，不依附 HTTP 连接 ★
  └─ 每产生一个事件 → 写 run_events → 推送给在线订阅者
GET  /runs/{id}/events?after_seq=N   SSE，先补发历史再续流
POST /runs/{id}/resume               中断确认
```

续流服务器先订阅唤醒通知，再按 `seq > cursor` 查库；
每次醒来和心跳超时都再查库。通知可以丢，数据库事件不能丢，
因此不存在“查完历史、订阅完成前”的空窗。

**普通对话也走 run**：非 Agent 的问答同样创建 `agent_runs`（goal = 用户问题）。
这样刷新恢复、断线续传、时间线渲染只有一套实现，
不必为"对话"和"任务"分别写两遍。

## 考虑过的替代方案

| 方案 | 优点 | 放弃理由 |
|---|---|---|
| **保留单表，把 step_idx 改成事件自增序号** | 改动最小 | 解决了冲突，但完全没有幂等能力——重放会重复执行副作用。只是把错误藏起来 |
| **靠 LangGraph checkpoint 天然去重** | 不写额外表 | checkpoint 恢复的是**状态**，不是**副作用**。节点重执行时外部写入照样发生。这是 LangGraph 文档明确提示的陷阱 |
| **把副作用工具全部移到节点末尾** | 规避重执行 | 脆弱的约定，靠人记住而非机制保证；且 HITL 场景下 interrupt 必然在工具之后 |
| **Redis 做幂等键** | 更快 | 幂等记录必须和业务数据同生命周期、可审计、进程重启后仍在。放 Postgres 与 run 同库，可用事务 |
| **SSE 不做恢复，刷新就重来** | 最简单 | 直接放弃 B1/B2/B3 三条边界需求，而这三条正是面试会问的部分 |

## 接受的代价

1. **表从 3 张变 6 张**，写入路径变长
   → 单用户场景下 IO 不是瓶颈
2. **`run_events` 会持续增长**
   → 完成超过 30 天的 run 归档或删除事件明细，保留 `agent_attempts` 汇总
3. **每个事件多一次 DB 写**，流式 delta 尤其频繁
   → delta 事件按 N 个 token 或 50ms 批量落库，只有结构化事件（citation/plan/step）逐条写
4. **seq 发号需要串行**
   → `UPDATE agent_runs SET next_seq = next_seq + 1 RETURNING`，
   单 run 由单 worker 处理，无并发竞争

## 后续影响

- `messages` 增加 `status`（streaming/completed/failed/cancelled）与 `run_id`
- 前端 SSE 信封统一为 `{id: "run:seq", run_id, seq, type, data}`，
  `id` 直接用作 SSE 的 `id:` 字段以支持 `Last-Event-ID`
- Agent 时间线组件从 `run_events` 渲染，与实时流共用一套解析逻辑
- `write_note` 等副作用工具必须在 `CLAUDE.md` 约束中明确要求走幂等协议
