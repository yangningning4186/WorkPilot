# 19 · Agent Harness 与 Pi 的对齐差距

> 状态：2026-08-29 首轮对照 + 三批收敛 + 第四轮复审 + 第五轮可插拔性/会话树收敛完成 +
> **第六轮复审（见 §14，只复审，未收敛）**。对照物是
> [earendil-works/pi](https://github.com/earendil-works/pi)
> 的 `packages/agent` 与 `packages/coding-agent`。
> 本文只记录**结构性差异**与可执行的收敛动作，不记录功能有无——功能面的对齐见
> [16-OpenWorker-P0-P1对齐](16-OpenWorker-P0-P1对齐.md)。
>
> **首轮结论**：已经对齐的是「有哪些模块」，没对齐的是「模块之间的缝在哪」。
> Pi 的精简不来自代码少，来自 **loop 只开两条缝（tool 前后、turn 前后），
> 所有产品策略都是插在缝里的小函数**。三批收敛（§9 P0-P6）已把这些缝物化。
>
> **第四轮发现**：缝已经开对了，但当时**缝上没接线**——provider 知道的事实没有传到
> harness，其中两处会静默产出错误结果；第四批已逐条接通（§12.9–12.10）。同时
> **「Pi 更精简」这个前提已经失效**（§0），行数不再是可用的标尺，新标尺只有一条：
> *哪些事实在层与层之间丢了*。
>
> **第六轮发现**：前五轮问的是「事实有没有传到」。这轮换了个问法：**事实没有到达时，
> 那一层默认相信了什么？** 三处默认相信了「成功」，其中两处会静默产出错误结果
> （§14.1、§14.2）。第六轮**只复审，未收敛**——§14 的动作项全部待办。

---

## 0. 体量对照

首轮这张表的口径是错的：它拿 Pi 的**框架包**比 WorkPilot 的**产品层**。第四轮按
「同一角色」重新对齐，结论随之翻转。

| 角色 | Pi | WorkPilot |
|---|---|---|
| 纯循环 | `agent-loop.ts` **796 行**（首轮记的 155 行已过时） | `agent_core/loop.py` 31 行 + `decide()` 23 行 |
| 工具执行 | `executeToolCalls` + sequential/parallel 两分支 | `execute_tool()` **126 行** |
| **产品承载类** | `coding-agent/core/agent-session.ts` **3495 行 / 97 个方法** | `_CoworkExecution` **2417 行 / 54 个方法** |
| 框架包 | `packages/agent/src` 12.6k 行 | `agent_core/` 2254 行 + `packages/workpilot-ai` 4.2k 行 |
| 未实现骨架 | `harness/agent-harness.ts` 的 lane / navigation / 队列全是 `HarnessNotImplemented` | — |
| 状态 | 由 record log reduce 出来，矛盾即拒绝 | 21 字段 RunConfig + 30 字段 v3 checkpoint；v1/v2 显式迁移，v3 形状不符即具名拒绝（§12.8） |

**所以「承载类臃肿」这条首轮批评已经不成立**：Pi 自己的产品层比我们更大。
`agent_core/loop.py` 是装饰这句仍然成立——但它现在装饰的是一个已经把策略拆成
gate / hook 的循环，代价可以接受。

行数不再有信息量。第四轮改用的判据是：**provider 层已知的事实，有没有传到做决定的那一层。**

---

## 1. Agent Loop

### Pi 的形状

`runLoop` 只知道四件事：turn 边界、tool call 批次、steering 队列、abort。其余全部
是 `AgentLoopConfig` 上的回调：

```
convertToLlm          AgentMessage[] → Message[]（唯一的协议压平点）
transformContext      压平前改上下文（裁剪、注入）
beforeToolCall        → { block, reason, terminate }
afterToolCall         → { content, details, isError, terminate }
shouldStopAfterTurn   → bool
prepareNextTurn       → { context, model, thinkingLevel }
getSteeringMessages   运行中插入
getFollowUpMessages   本该停下时续跑
```

早停统一由 `AgentToolResult.terminate` 表达，且**整批都 true 才停**——这条规则写在
一个地方，不是散在每个策略里。

### WorkPilot 的形状

`decide()` 顺序内联了至少 18 段互不相干的策略：

steering 消费 → 工具裁剪 → plan 模式过滤 → system prompt 组装 → 临时块组装 →
压缩 → 模型调用 → overflow 恢复循环 → 文本工具调用抢救 → 空回答判失败 →
未执行文本调用判失败 → late steering → 引用校验 + 修复 → 未加载工具拒绝 →
重复调用熔断 → plan 门 → sleep 门 → interaction 门 → shell 审批 →
外部审批 → 语义复核 → 独占门。

**代价是可量化的**：`{"role": "tool", ...}` 的 denial 消息在 `runtime.py` 里手工拼了
**19 次**，「本批调用均未执行」出现 **7 次**，「必须单独调用」出现 **11 次**。

第一批收敛后，策略 denial 只在 `_tool_error_message` 一处编码，整批拒绝只从
`_deny_batch` 一处完成计数、checkpoint 和 `tool.error`。第二批进一步把可用性、重复熔断、
plan、独占、sleep、interaction、shell 审批和外部审批组成有序 gate 链；`decide()` 最终从
830 行降到 23 行。剩余的 `role=tool` 字面量属于人工恢复结果、sleep 成功结果和真实工具
执行结果，不再是复制的策略拒绝。

最尖锐的一处：`runtime.py:2787` 已经有一段通用的
`registry.is_exclusive()` 独占检查，而 **sleep（2461）、interaction（2489）、
run_shell（2522）、外部审批（2687）各自又手写了一遍同样的四段代码**——
拒绝一批 → 写 tool 消息 → `iteration +=` → checkpoint + 发 `tool.error`。

> **为什么会长成这样**：因为没有「策略」这个类型。每加一条规则唯一的落点就是
> 在 `decide()` 里再加一个 `if`，而每个 `if` 都必须自己负责一遍消息拼装、
> 计数、落盘和发事件。这是**缺少接缝导致的机械重复**，不是逻辑复杂。

---

## 2. 模型调用

Pi 的 `StreamFn`（`types.ts:28`）有一条硬契约：

> **绝不 throw**。请求/模型/运行时失败一律编码成 `stopReason: "error" | "aborted"`
> 的 AssistantMessage。

于是 loop 里处理失败只有一行 `if (message.stopReason === "error") { ... return }`。

WorkPilot 这边 `ModelContextOverflowError` / `ProviderContextOverflowError` /
`RunBudgetExceededError` 都是异常，因此 `decide()` 被迫写了一个
`while True: try/except` 的 overflow 恢复块（2120-2150），并且**几乎每个
`await self._checkpoint` 之前都要再 catch 一次预算异常**。

| | Pi | WorkPilot |
|---|---|---|
| 失败表达 | 消息上的 `stopReason` | 主决策已收成 `ModelTurnResult.stop_reason`；次级补全仍有异常边界 |
| 流式 | 一等公民，delta 走同一条事件流 | `CompletionResult` 是完成态；delta 走旁路 `CoworkStreamSink` |
| 换模型 | `prepareNextTurn` 每轮可换 model / thinkingLevel | run 内固定 |
| 网关职责 | `pi-ai` 只做 provider 归一 | `gateway.py` 1086 行兼做路由/预算/定价/缓存/审计 |

**异常当控制流是 `decide()` 变长的主因之一**，优先级仅次于缺接缝。

---

## 3. 工具系统

Pi 的 `AgentTool` 只有 6 个字段，但 `AgentToolResult` 做了一个关键分离：

```ts
{ content,        // 给模型看的
  details,        // 给 UI / 日志看的结构化数据
  usage, addedToolNames, terminate }
```

首轮时这个通用分离还没有：`_encode_tool_result` 把 output 与部分执行元数据揉进同一个
JSON 塞给模型。第二、三批已完成 `CoworkToolResult.content/details` producer 迁移；原有的
`evidence` / `effect_ref` / `authorization_receipt` 继续走旁路。第四批又增加独立
`attachments` 通道，二进制内容不再被揉进 JSON，浏览器截图可在下一轮作为 image
attachment 交给 provider。

反过来，WorkPilot 的 `RegistryToolSpec` 有 12 个轴（`risk` / `effect` /
`parallel_safe` / `execution` / `approval_required` / `exclusive` / `deferred` /
`search_aliases` …），描述力**远超** Pi。第一批已把独占规则统一到 metadata，第二批又把
策略特判移进命名 gate；`run_shell` / `sleep` 等仍有各自 adapter，但 loop 不再认识这些
工具名。剩余问题是把更多动态行为继续下沉到 spec / handler，而不是继续扩 gate。

> 判据应该是元数据不是工具名——这条你在别处（plan 模式按 `risk`/`execution` 而不是
> 名单准入）已经做对了，工具执行边界上没有贯彻。

---

## 4. 消息系统

Pi 有两层类型：

```
AgentMessage = Message | CustomAgentMessages[...]      // 会话里流转的
Message                                                 // provider 收得下的
```

自定义消息通过 declaration merging 扩展出 `bashExecution` / `custom` /
`branchSummary` / `compactionSummary`，**`convertToLlm` 是唯一一处把它们压平的
地方**，UI-only 的直接 filter 掉。

WorkPilot 首轮的 `CoworkMessage` 就是 OpenAI 形状，没有非 LLM 消息的位置。两个可见
症状是：

1. 压缩摘要存在 `CompactionState` 里而不是消息里，于是 canonical 历史和 outbound
   视图必须靠 `build_outbound_messages` 每轮重新缝
2. **citation 修复指令被伪装成 `role: "user"` 加 XML 标签**。
   代码里那段注释自己在解释「为什么不能用 system」——真正的答案是缺一层
   `runtime_directive` 消息类型

第二批已新增 `agent_core.messages.AgentMessage` 和唯一 `convert_to_llm()`：citation 修复
以 `runtime_directive` 留在 canonical，provider 边界才压成兼容形状；`custom` 可留给 UI
并默认从模型视图过滤。压缩摘要仍留在 `CompactionState`——这是保留 canonical 不可变与
可审计视图的有意取舍，但 outbound 中已经先表达成 `compaction_summary` 再统一压平。

---

## 5. 事件驱动

Pi 的 `AgentEvent` 是 9 个成员的封闭 union，**流式 delta 也在里面**
（`message_update` 携带 `assistantMessageEvent`）。`HarnessEventBus.watch()`
返回 `{ snapshot, start, unsubscribe }`：先缓冲、`start()` 时 flush，
不用数据库就解决了「订阅与快照之间的竞态」。

WorkPilot 首轮：存储 DTO 的 `RunEvent` 是 `(type: str, payload: dict)`，事件名以字符串
字面量散在生产者中；流式 delta 走**另一条路**（`CoworkStreamSink`：reset / text /
reasoning / drain）。两条通道、两套心智。

这里也有一条现状校正：API schema 和前端原本各有一份 `RunEventType Literal`，所以不是
完全没有 union，而是 **union 没有成为 source of truth**——store/runtime 仍收 `str`，
新增的 `approval.semantic_review`、Persona 与 Team/Board 事件已经与两份 union 漂移。
第一批已把事件名提升到共享 `app.run_events`，让存储 DTO、runstore、runtime emitter、
API schema 共用同一封闭类型，并补了核心 payload TypedDict；逐生产者建立
「事件名 → payload」的静态对应仍待完成。

> **这一块要说清楚哪边强**：`run_stream.py` 的持久化 + `Last-Event-ID` 断线续传
> 比 Pi 的纯内存 bus 强，而且是服务端产品必需的。**不要拆它**。要补的只是把
> 事件名收成封闭 `Literal`、payload 上 TypedDict，让流式 delta 也进同一个 union。

---

## 6. 上下文工程 —— WorkPilot 领先

稳定前缀（environment / standing rules / memory / persona / mode /
skill countermand / session facts / locate / knowledge，**run 起始冻结**）
+ 尾部 `ephemeral_suffix`（todos / roots / capabilities / reading viewport /
loaded tools，**每轮重算**）。分界依据是「这一次 run 里会不会变」，
目的是 provider 前缀缓存——Pi 没做到这个程度，它的 `systemPrompt` 就是个字符串。

组装方式的债已在第四批收敛：`build_turn_context()` 返回冻结的 `TurnContext`，压缩器
显式接收 `system_prompt` / `tools` / `ephemeral_suffix` 参数，不再通过
`self.compactor.x = ...` 传递隐式状态。稳定前缀与每轮变化的 suffix 分界保持不变。

---

## 7. 上下文压缩

| | Pi | WorkPilot |
|---|---|---|
| 触发依据 | `getLastAssistantUsage()`（`compaction.ts:184`）——**provider 报的真实 usage**，估算仅兜底 | 已改为 provider usage + 新增尾部局部估算；无 usage / revision 变化时才全量估算 |
| 切点 | `findCutPoint`（`compaction.ts:374`）只在合法 turn 边界切，永不劈开 tool_call/tool_result 对 | `_target_boundary` 按 boundary index |
| 落点 | 写一条 `CompactionEntry{summary, retainedTail, tokensBefore}` 进日志，可回放可导航 | 滚动 `summary` + `summary_upto` 存进 `CompactionState` |
| 降级 | 独立的 `branch-summarization.ts` | 强制重跑 + `tool_content_max_chars` 折半 |

WorkPilot 的「canonical 不可变 + 每轮重建 outbound 视图」在可审计性上**比 Pi 好**，
这是个正确的取舍，别改。首轮有两个实际问题：

1. **触发用估算会长期漂移**。第二批前已改成真实 usage 加新增尾部局部估算
2. 每轮仍会重建 outbound 视图，但不再每轮对全量 canonical 做 token 重估；视图构建本身
   仍是后续可 profile 的固定开销

切点安全已经比首轮描述更接近 Pi：`_complete_history_boundaries()` 会先验证 assistant
tool calls 后紧随的 tool results 是否齐全，`_target_boundary()` 只从这些边界选择，
不会劈开 call/result 对。现在真正未对齐的是**压缩落点是否是一等日志条目**，不是触发
信号或 tool 边界完整性。

---

## 8. 会话管理 —— 最大的结构性分叉

Pi 把两件事分开了：

- **Entry**（`session/types.ts`）：模型看得见的东西，append-only，靠 `parentId`
  串成树。类型有 `message` / `model_change` / `thinking_level_change` /
  `active_tools_change` / `compaction` / `branch_summary` / `custom`
- **LaneRecord**：harness 做过什么的**意图日志**——`operation_started` /
  `operation_finished` / `step_attempt` / `tool_started` / `queue_enqueued` /
  `abort_requested` / `write_deferred`

`reducer.ts` 把 records 回放成 `LaneState`，遇到 **12 种命名的
`RecordLogCorruption`** 之一就**拒绝继续而不是修复**（`reducer.ts:22-34`）——
和我们「换了 embedding 就拒绝检索」（约束 10）是同一条哲学：无声失败比显式失败贵。

WorkPilot 拆分前的 `StateCheckpoint[CoworkState]` 是整份 JSON 快照，当时
`CoworkState` 有 **49 个字段**，还带着 `_upgrade_v1_state` 迁移。第三批已把物理存储拆成单行
`CoworkRunConfig` 与 append-only `CoworkCheckpointState`；运行时仍由 Store 重组完整视图，
因此恢复与调用方不需要知道两张表。没有分支、导航或 fork 的产品选择未变。

> **关键差异不是「有没有分支」**——单用户产品可以不要 lane。
> 关键是 Pi **把「模型看到的」和「harness 做过的」分成两种记录，状态是推导出来的**；
> WorkPilot 把两者揉进一个 TypedDict，所以它只能越长越大，且每个 checkpoint 都要
> 把 run 内根本不变的 `persona_*` / `capability_*` / `work_mode` / `locate_block` /
> `knowledge_block` / `session_facts` 重新序列化一遍。

---

## 9. 收敛动作（按投入产出比排序）

### P0 · 把 denial 收敛成一个函数（已完成第一批）

```python
def _deny_batch(
    state: CoworkState,
    calls: Sequence[ToolCall],
    *,
    reason: str,
    event_tool: str,
) -> Awaitable[CoworkState]:
    """拒绝整批调用：写 tool 消息 + 计数 + checkpoint + tool.error。"""
```

策略 denial 已从 `_tool_error_message` + `_deny_batch` 两个窄出口物化；`decide()` 从
830 行降到 700 行。interaction、静态审批和显式 `exclusive` 现在统一由
`registry.is_exclusive()` 判定，`run_shell` 的独占性也已写回 spec。动态审批仍需等参数
preflight 后才能判定，所以保留一条审批 gate，而不是伪装成静态元数据。

### P1 · 给 ModelGateway 包一层不抛异常的边界（Cowork 决策入口已完成）

新增 `agent_core.model_turn.run_model_turn()`：主决策的 overflow / budget / 规范化
provider error 变成 `ModelTurnResult.stop_reason`，`decide()` 的模型调用不再用
`try/except` 分派控制流。整条 provider route timeout 也先编码成结果，再由 worker
适配回既有的 checkpoint 有界重试；任务取消和编程错误仍传播，避免吞掉真实 bug。

第二批又把强制收尾入口接到同一契约；摘要器本来就有独立的确定性 fallback，不需要为了
形式一致改成同一套终态。Cowork runtime 里的两个决策模型入口现在都不再用 provider 异常
分派正常控制流。

### P2 · 引入 `before_tool_call` / `after_tool_call` 两条缝（已完成第一版）

把 plan 门、重复熔断、独占检查、未加载工具拒绝、审批门、语义复核全部改写成

```python
ToolGate = Callable[[ToolGateAllow], Awaitable[ToolGateDecision]]
# ToolGateDecision = ToolGateAllow | ToolGateBlock | ToolGatePause
```

`before_tool_call()` 现在按固定顺序执行 9 个带稳定 id 的 gate（新增 schema 前的
`prepare_arguments` 规范化）；loop 的
`_materialize_tool_gate()` 只负责把 `Block.reason` 变成 error tool result、把 `Pause`
变成 durable sleep / `waiting_human`。重复熔断保留了“只拒重复项、其余继续”的批次语义。

成功工具结果则经过 4 个可异步 `after_tool_call` hook：证据登记 → canonical tool result / 事件 →
runtime state 投影 → artifact 投影。`decide()` 已降到 **23 行**，`execute_tool()` 降到
**127 行**。下一层可选收敛是把这些已经命名的 Cowork 策略搬出 `_CoworkExecution`；这只
影响文件组织，不再影响 loop 形状。

### P3 · 加 `AgentMessage` 层与 `convert_to_llm()`（已完成）

共享 `agent_core.messages.AgentMessage` 已增加
`"compaction_summary" | "runtime_directive" | "custom"`；`convert_to_llm()` 是唯一协议
压平点，UI-only custom 默认 fail-closed 过滤。citation 修复现在以 canonical
`runtime_directive` 落 checkpoint，到 provider 边界才转换成兼容的 user 消息；新 run 不会
继承旧 run 的 runtime directive。

`CoworkToolResult` 已完成全部 producer 迁移：`content` 是必填的 provider-facing 字段，
`details` 只供 UI / 日志使用；新写入的 invocation 结果不再保存 `output`。`.output` 仅保留为
读取 dict content 的兼容属性，旧 invocation row 的 `output` 也只在 replay 边界解码，不能再被
任何 handler 构造。AST 扫描覆盖 `app/`、`tests/`、`eval/`，legacy 构造器为 **0**。

### P4 · 压缩触发改用真实 usage（已完成）

`CompactionState` 已记录 `last_input_tokens`、对应 canonical message count、revision 与当时
的工具 schema 量。下一轮以真实 usage 加其后新增消息和新增工具 schema 的局部估算触发；
没有 usage、usage 无效或压缩 revision 变化时才用全量估算。`context.compacted` 同时记录
`trigger_source` 与 `trigger_tokens`。

### P5 · 事件名与 payload 收成 discriminated union（已完成）

后端 `RunEventInput` 已把 44 个 `Literal[event_type]` 逐项绑定到对应 payload `TypedDict`；
动态工具进度先形成 `RunEventDraft`，在唯一 `run_event()` 边界校验事件名并收敛进封闭 union，
SQLite 只接收这个边界验证后的事件。前端同步以 `RunEventPayloadMap` 生成
`StreamEnvelope` discriminated union，`type` 分支会自动收窄 `data`。SSE 持久化与
`Last-Event-ID` 续流未动。

### P6 · 拆 `CoworkState` / `RunConfig`（已完成）

`CoworkRunConfig` 现有 21 个冻结字段，`CoworkCheckpointState` 有 30 个可变字段；
`CoworkState` 只是两者的内存合并视图。SQLite schema v22 新增
`cowork_run_configs(run_id PK, config, created_at, updated_at)`，每个 checkpoint 只序列化
可变状态。`locate_block` / `knowledge_block` 在首轮 pre-loop 冻结时只在值变化后更新这唯一
一行，不复制到后续 checkpoint。Store 的 load 边界透明重组两者；v21 及以前的整份
checkpoint 没有 config row 时仍可读，下一次保存才自然拆分。

---

## 10. 不要动的三块

对齐 Pi 不等于抄 Pi。这三处 WorkPilot 明确更强，收敛时必须原样保留：

1. **前缀缓存导向的上下文装配**（§6）——Pi 没有这个概念
2. **SSE 持久化 + `Last-Event-ID` 断线续传**（`run_stream.py`）——Pi 的 bus 是纯内存
3. **`tool_invocations` 幂等租约与 `outcome_unknown`**（[ADR-0007](adr/0007-agent幂等与事件溯源.md)）
   ——Pi 只有 `replay: "never" | "safe"` 一个标记，不做跨系统 effectively-once

首轮预估曾认为 P0-P5 能把 `_CoworkExecution` 压到 600-800 行；第二批实践表明，先命名并
测试策略会让承载类暂时变长，真正下降必须再把 gate、模型回合装配和 HITL adapter 搬到独立
policy/service。当前更有意义的阶段指标是 `decide()` 830 → 23、`execute_tool()` 228 →
127；下一批再处理文件归属，不为追行数碰以上三块。

---

## 11. 验证记录（2026-08-28）

- 新增模型回合边界与真实 usage 压缩单测；`17 passed`
- 独占批次、provider overflow 恢复、overflow progress guard、route timeout checkpoint
  重试定向回归；`5 passed`
- run event 持久化、SSE 续流与 emitter 时序回归；`23 passed`
- 变更范围 Ruff、strict mypy、8 条 import-linter 架构契约通过；前端
  `npm run typecheck` 通过
- 完整后端复跑：`1247 passed`（5 条第三方 SWIG deprecation warnings）

第二批新增 gate/hook、AgentMessage 转换和 tool `content/details` 单测；审批、shell、sleep、
interaction、重复熔断、压缩与 citation 定向回归 **102 passed**。
第二批完整后端复跑：**1252 passed**；strict mypy 覆盖 **205** 个 source files，Ruff、
8 条 import-linter 架构契约与前端 `npm run typecheck` 全部通过。

第三批完成 P3 producer、P5 payload union 与 P6 RunConfig 拆分；P3 AST 扫描 legacy
构造器 **0**，P5/P6 定向回归与核心 Cowork 回归 **118 passed**。完整后端复跑
**1255 passed**；strict mypy 覆盖 **207** 个 source files，Ruff、8 条 import-linter
架构契约与前端 `npm run typecheck` 全部通过。

第四批完成 P7-P13：三 provider 终止原因、截断批次零执行、shell 尾部/完整 Artifact、
截图 attachment、v3 corruption/MissingIdentities、引用升档、显式 TurnContext 与 typed span
均有定向测试。完整后端复跑 **1268 passed**；strict mypy 覆盖 **229** 个 source files，
Ruff、8 条 import-linter 架构契约、`git diff --check` 与前端 `npm run typecheck` 全部通过。

四批均未改 SSE / Last-Event-ID、checkpoint append-only 策略或工具幂等租约。

---

## 12. 第四轮复审与收敛（2026-08-29）：provider → harness 的信息丢失

前三批解决的是「策略没有落点」。这一轮换了个问法：**每一层已经知道的事实，有没有
传到需要它做决定的那一层？** 按这个问法查下来，前三批修好的接缝上有三根线没接。

### 12.1 根因：`CompletionResult` 不带终止原因

```python
# packages/workpilot-ai/src/workpilot_ai/types.py:71
@dataclass(frozen=True)
class CompletionResult:
    text: str
    model: str
    provider: str
    usage: Usage = field(default_factory=Usage)
    tool_calls: tuple[ToolCall, ...] = ()
    # ← 没有 stop_reason
```

Provider **明明拿到了**：`providers/anthropic.py:323` 把 `stop_reason` 收进局部变量，
只在 `:339` 拼错误消息时用一次就丢掉；`openai_compatible.py` 的 `finish_reason` 同理，
只出现在异常文案里（`:217`、`:540`）。

于是 harness 分不清「模型说完了」和「模型被 `max_tokens` 砍断了」。后果按严重度：

1. **半截答案被判成功。** 正文写到一半被截断 → `_handle_text_completion`
   （`runtime.py:2740`）只检查空文本 → 半截 `final_message` 落 checkpoint、
   run 标记 done，界面上没有任何迹象。
2. **残参工具照常执行。** 一批 3 个调用里前 2 个完整、第 3 个被截断，前两个会执行；
   模型本来的计划是三步。

Pi 在 `agent-loop.ts:212` 上有一条硬规则，注释里写明了理由——流式 tool-call 参数是
salvage 解析器拼出来的，**截断后仍可能解析成功且通过 schema 校验，但内容是残的**：

```ts
const executedToolBatch =
    message.stopReason === "length"
        ? await failToolCallsFromTruncatedMessage(toolCalls, emit)   // 整批不执行
        : await executeToolCalls(currentContext, message, config, signal, emit);
```

> **这条踩的是自己的哲学。** 约束 10 说「无声失败和显式失败的区别，是这条约束的全部
> 理由」；`KbIndexVersion` 不匹配就拒绝检索。截断回合是同一类问题的另一个实例，
> 只是这次沉默的那一层是 provider adapter。

**已修复。** `CompletionResult.stop_reason` 现为封闭
`"stop" | "length" | "tool_use" | "error"`，Anthropic / OpenAI-compatible / Gemini
三只 adapter 都保留原生终止事实。`ModelTurnResult` 新增 `truncated`；正文截断不会交付，
工具截断会为批内每个 call 写 error result 且零执行。连续三次截断进入显式失败，任一完整
回合会清零连续计数。

### 12.2 模型调用：另外两条

- **`run_with_escalation` 是死代码。** 全仓唯一引用是 `gateway.py:922` 的一句注释。
  [docs/07 §3](07-模型路由与成本.md) 承诺的置信度升档在产品里一次都没被调用过。
  引文校验失败是天然触发点（`EscalationRejected(reason="quote_mismatch")`），
  要么接进 `_request_model_decision`，要么在 07 里降级成「设计未启用」——
  不能留着一个能被追问「升档率是多少」的空承诺。
- **run 内不能换档/ 换 thinking level。** Pi 每轮 `prepareNextTurn` 可换 model 与
  thinkingLevel。我们的 overflow 恢复只会压缩，不会「换一个大窗口的档位重试」。
  优先级低，但 escalation 接上之后是顺手的事。

`run_with_escalation` 已接入 Cowork：第一次引用校验失败仍走同档 repair；repair 草稿再次
未通过免费的确定性引用校验时，按 `routing.yaml` 的 `cowork_decision: heavy` 重做一轮。
普通回合仍为 main，provider 调用失败仍走 fallback，两种语义没有混用。thinking level
仍未做 provider-neutral 动态切换，保留为低优先项。

### 12.3 工具系统：三处判据错位

| 问题 | 位置 | Pi 的做法 |
|---|---|---|
| 工具结果只能是文本 | `cowork/tools.py:392` `content: dict \| str` | `AgentToolResult.content: (TextContent \| ImageContent)[]` |
| shell 截断留了错的半边 | `cowork/shell.py:323` `_read_limited` 保留**前** 64KB；`runtime.py:790` `_encode_tool_result` 再一次头部截断 | `utils/shell-output.ts` 用 `truncateTail` 保**尾**，完整输出落盘给 `fullOutputPath` 供 agent grep |
| 结果级错误按工具名判 | `runtime.py:830` `_result_level_error`：`if tool != "run_shell": return None` | — |

**修复前截图是废的**：`browser_tools.py:565` 截完图只把 `path` 回给模型，模型永远看不到
那张图。对一个主打沉浸阅读 + 浏览器操作的产品这是功能缺口，不是洁癖——扫描版 PDF 页、
「渲染有没有错位」，现在都问不了。`MessageAttachment` 已经支持 image 入站，
缺的只是 tool result → attachment 这一段。

**shell 那条更直接**：编译错误、traceback、`pytest` 的 `5 failed` 汇总行**全在尾部**。
现在一次长跑批，模型拿到的是收集阶段的刷屏，看不到失败原因，然后开始瞎猜。

`_result_level_error` 按工具名判则是第三批批评过的模式换了个地方——从 gate 挪到了
结果编码。判据应该是 spec 上的元数据（如 `result_error_probe`），不是 `if name ==`。

**三项均已修复。** `CoworkToolResult.attachments` 通过 canonical
`runtime_directive` 在唯一 `convert_to_llm()` 边界转换为 provider image attachment；
`browser_screenshot` 已接通。shell 内存短视图与最终 JSON 编码都保留尾部，完整输出写入
授权根目录下的有界 `.txt` 并登记为 Artifact，模型可用 `search_files` / `run_shell grep`
回查。结果级失败由 `CoworkToolSpec.result_error_probe` 判定，编码策略由
`result_encoding` 判定，runtime 不再认识 `run_shell` 名字。

### 12.4 消息系统：已对齐

`AgentMessage` + 唯一 `convert_to_llm()` + `runtime_directive` 落地干净，
`CoworkToolResult.content/details` 的 producer 迁移是真的做完了（`output` 只剩兼容读
属性）。剩下的多模态缺口属于 §12.3，不在这里重复。

### 12.5 事件驱动：已经超过 Pi，但缺 span

`message.delta` / `message.reasoning` 进了同一个 44 成员封闭 union（`run_events.py`），
payload 逐项绑 TypedDict，再叠 SSE 持久化 + `Last-Event-ID`。Pi 的
`harness/events.ts` 只有 `run_start` / `run_end` 两个成员且是纯内存。**这块别动。**

缺的是**另一件事**：Pi 有一套声明式 telemetry schema（`harness/telemetry.ts` 615 行 +
`packages/telemetry`，带 conformance 测试），span 名、属性、基数都是类型化的，
`pi.ai.request` 上就带 `response.stop_reason`（注意：连这个字段都在他们的 schema 里）。
我们这边 `core/trace.py` 只有请求级 `trace_id` 绑进 structlog，没有
run → turn → tool 的 span 树，「这个 run 为什么慢」只能人肉翻日志。

**已补齐。** `workpilot_telemetry.spans` 封闭声明 `agent.run` / `agent.turn` /
`agent.tool` 与三类 discriminated attributes；SQLite 新增 `agent_spans`。触发工具批次的
`last_turn_span_id` 进入 checkpoint，因此跨 worker 恢复后的 tool span 仍挂在原 turn 下。
`llm_calls` 同时记录 request `span_id`、`parent_span_id` 与 provider `stop_reason`。

### 12.6 上下文工程：仍领先，副作用组装通道已移除

稳定前缀冻结 + `ephemeral_suffix` 每轮重算，Pi 没有这个概念（`systemPrompt` 是字符串
或回调）。首轮指出的问题原样还在：`runtime.py:2618-2654` 靠
`self.compactor.system_prompt = ...` / `self.compactor.tools = ...` **给压缩器传参**。

`_request_model_decision` 137 行里塞了六件事——工具裁剪、prompt 组装、压缩、
overflow 恢复循环、文本调用抢救、usage 记账——现在是全类第二长的方法
（最长的是 `_standing_approval` 193 行）。等价形状是收成不可变的
`build_turn_context(state) -> TurnContext`，压缩器接参数而不是接字段。

**已修复。** `TurnContext` 是冻结 dataclass；工具面、system prompt 与 ephemeral suffix
在 `build_turn_context()` 一处装配，`OutboundCompactor.prepare/build` 只接显式参数。

### 12.7 上下文压缩：触发与切点已对齐

真实 usage + 尾部局部估算、`_complete_history_boundaries()` 保证不劈开 call/result 对，
与 Pi 的 `getLastAssistantUsage` / `findCutPoint` 同一水位。

剩一条低优先：`CompactionState.summary` 是**滚动覆盖的单字段**，
`context.compacted` 事件只带指标不带摘要正文（`runtime.py:3502`）。Pi 每次压缩追加一条
`CompactionEntry{summary, retainedTail, tokensBefore}`。我们的旧摘要能从更早的
checkpoint 反推（checkpoint 是 append-only），所以不是审计断链，只是查起来要绕。

### 12.8 会话管理：现在这里是最大的结构性分叉

Pi 把状态**推导**出来：`reducer.ts` 回放 record log 成 `LaneState`，遇到 12 类具名
`RecordLogCorruption`（`reducer.ts:22-34`）之一就**拒绝恢复**。

`load_cowork_checkpoint`（`runtime.py:1298-1376`）是 **18 处 `setdefault` + 14 处
normalize/强制赋值**的静默修补带，外加还活着的 `_upgrade_v1_state`。

> 这不是「字段多」的问题，是**方向反了**：在所有别的地方都选择显式失败
> （约束 10、索引签名不匹配就拒检索），只在恢复边界选择了猜。一个字段缺失可能意味着
> 「老版本写的」，也可能意味着「上次写 checkpoint 时崩在一半」——现在这两种被同一句
> `setdefault` 抹平。

同族的还有一条：历史里引用了已经下线的工具时，`activate_tools`
（`agent_core/tools.py`）只是 `if name in self._tools` 静默过滤；Pi 在 resume 时返回
`MissingIdentities{tools, models}` 并拒绝续跑。

第四批结束时还没有 fork / 分支；第五批已补为 copy-on-fork 会话分支，见 §13.4。

**已修复恢复边界。** 当前 schema 升为 `cowork.v3`：只有已知 v1/v2 会进入显式迁移；
v3 缺字段、多字段、类型错误、不可无损 normalize 都抛带稳定 `code` 的
`CoworkCheckpointCorruptionError`。runtime snapshot 引用当前 registry / gateway
不存在的工具或 provider/model 时抛 `MissingIdentitiesError{tools, models}`，不再静默过滤。

### 12.9 收敛动作（第四批，按投入产出比）

| | 动作 | 成本 | 判据 |
|---|---|---|---|
| **P7 · 完成** | `CompletionResult.stop_reason` 打通到 `ModelTurnResult`（新增 `truncated` 终态）：`length` → 整批拒绝复用 `_deny_batch` + 半截答案不判 done | 3 个 adapter + 一个终态 + 一个分支 | 静默错误已封死 |
| **P8 · 完成** | shell 输出改留尾 + 完整输出落 Artifact 供 grep；`_encode_tool_result` 对 shell 类内容同步 | tail buffer + 有界文件 | 长命令保留失败原因 |
| **P9 · 完成** | tool result 支持 image attachment（复用已有 `MessageAttachment` 通道） | 中 | 截图已进入下一轮模型上下文 |
| **P10 · 完成** | 已知旧版本显式迁移；未知/损坏形状具名拒绝；补 `MissingIdentities` | 中 | 恢复边界与约束 10 同哲学 |
| **P11 · 完成** | 引用 repair 再失败触发 `run_with_escalation` | 小 | 死代码已进入产品路径 |
| **P12 · 完成** | `build_turn_context()` 取代 compactor 副作用；结果错误/编码改按 spec 元数据 | 中 | 接口显式化 |
| **P13 · 完成** | run → turn → tool 的 typed span | 中 | 可按层归因延迟与失败 |

§10「不要动的三块」在第四轮全部复核有效，其中第 3 条（幂等租约）领先幅度还扩大了：
Pi 至今只有 `replay: "never" | "safe"` 一个标记，不做跨系统 effectively-once。

### 12.10 第四批结论

这批没有继续追 `_CoworkExecution` 行数，而是逐条补齐事实传递链：

```
provider finish reason ─→ CompletionResult ─→ ModelTurnResult ─→ harness terminal
tool binary result      ─→ MessageAttachment ─→ AgentMessage ─→ provider attachment
run/turn/tool identity  ─→ typed span ─→ SQLite telemetry
checkpoint schema       ─→ explicit migrator/validator ─→ resume or named failure
```

当时仍保留的差异是：不做 lane/fork/navigation；暂不抽象
provider-neutral thinking level；压缩摘要仍从 append-only checkpoint 反推而不是另加
`CompactionEntry`。前缀缓存装配、SSE 续传和工具幂等租约均未改。

---

## 13. 第五轮收敛（2026-08-29）：可插拔性、旁路调用与会话分支

这轮复核确认两条早先判断已经过时：工具执行中途进度原本就通过 `tool.progress` 持久事件
接通 UI；run/turn/tool span 与属性类型也已经存在。真正缺失并已修复的是以下部分。

### 13.1 Provider 瞬时故障不再直接触发 fallback

OpenAI-compatible、Anthropic 与 Gemini 的 HTTP 响应统一区分暂时性 429/408/409/425/5xx
与配额耗尽 429。`ModelGateway` 在同一 endpoint 内先做有上限指数退避，尊重秒数或 HTTP-date
形式的 `Retry-After`；只有重试耗尽才进入既有 fallback。complete、tool stream、plain stream
与 embedding 共用该策略。重试次数和退避上下限进入 `Settings`，默认 2 次、0.5–8 秒。

压缩、标题、记忆抽取/分类、Skill 蒸馏与语义审批等旁路调用显式使用
`cache_retention="none"` 和独立 session id，不读写精确缓存或 provider prompt cache。

### 13.2 Loop 与工具策略改为带 id 的注册表

`AgentLoopConfig` 现包含 turn/context/steering/follow-up/tool 前后八个类型化回调；内层处理
模型与工具，外层处理 follow-up。Cowork 侧新增 `CoworkHookBus`，注册项按 `(order, id)`
稳定排序并拒绝重复 id，worker 可注入 configurator。原 8 个私有 gate 加上新的
`prepare_arguments`、4 个 after-tool 投影、`transform_context`、
`before_provider_request`、`before_compaction` 均走该总线。

工具 spec 新增 `prepare_arguments`、`prompt_snippet`、`prompt_guidelines` 和
`execution_mode="sequential"`。兼容参数在 schema、权限、审批、重复签名和执行之前只规范化
一次；工具自带指导只为当前实际暴露的工具渲染；sequential 会关闭批次并行。

### 13.3 压缩续传确定性操作台账

`CompactionState.details` 从已归档的 tool protocol 机械提取 `read_files`、
`modified_files` 与 `artifacts`，不依赖摘要模型；数量、单项长度和渲染字符数均有独立上限。
压缩仍保持 outbound-only，canonical 不修改。每次压缩同时追加一等 `compaction` session
entry，记录 cause、revision、边界、before/after/trigger token 与操作台账。

工具结果截断改为 2,000 行 / 50KB 双上限：文本 content 与 shell tail 只在完整行边界截断，
JSON 信封始终合法，不再从一行或多字节字符中间切开。

### 13.4 Session entry、分支与存储契约

SQLite schema v23 新增 append-only `session_entries(parent_id, seq, kind, payload)` 与命名
`session_lanes`。entry 类型封闭为 message / model_change / thinking_level_change /
active_tools_change / compaction / branch_summary / custom；消息、实际模型身份、延迟工具集变化
与压缩都会按发生顺序落 entry，而不再只能从 checkpoint blob 猜时间线。

`validate_session_tree()` 对重复节点、悬空 parent、环、序号、lane head、终态后追加、payload、
kind、conversation、根、消息引用和分支位置给出稳定 corruption reason，拒绝猜测修复。
`assert_cowork_store_conforms()` 是任意 Store 后端可复用的树/lane 回放契约套件。

会话 API 新增 entry 时间线读取和从任意已完成消息 before/after fork。fork 保留源会话，复制
边界前的可见消息，并追加包含来源与被舍弃尾部预览的 `branch_summary`；运行中的会话拒绝
分叉。桌面端用户消息上提供“从这里重试”，创建独立分支并把原消息放回输入框。

### 13.5 Telemetry 属性契约

费用 AuditRecord 与 agent.run/turn/tool span 均有封闭 `AttributeSpec`：声明 Python 类型、
required、cardinality、sensitive 与 allowed values；SQLite 写入前强制验证，测试锁定 schema
字段集合，避免成本治理字段静默漂移。

第五轮验证：新增/修改专项测试 **195 passed**；完整后端 **1290 passed**；产品源码
**232 files** strict mypy、后端全量 Ruff、8 条 import-linter 契约、前端定向 ESLint 与 TypeScript
typecheck 全部通过。

---

## 14. 第六轮复审（2026-08-29）：事实缺席时的默认值

> 本节先保留复审时的原始判断；实际核验、修正和有意暂缓项见 **§14.7**。

第四、五轮把「provider 已知的事实有没有传到做决定的那一层」查完了。这轮换的问法是：

> **事实没有到达那一层时，它默认相信了什么？**

按这个问法查下来，有三处默认相信了「成功」，其中两处会静默产出错误结果——和约束 10
（「无声失败和显式失败的区别，是这条约束的全部理由」）是同一类问题的第三、第四个实例。

### 14.1 传输层截断被当成正常完成（严重）

三个流式 adapter 都不校验**终止事件是否到达**：

| adapter | 终止事件 | 现状 |
|---|---|---|
| `providers/anthropic.py:237` `stream_with_tools` | `message_stop` | 从不检查；`stop_reason` 保持 `None` |
| `providers/openai_compatible.py:711` | `[DONE]` | `if not data or data == "[DONE]": continue` —— 当成可跳过的空行 |
| `providers/gemini.py` | `finishReason` | 缺失即空串 |

而三个归一函数把「缺失」一律映射成成功：

```python
# providers/anthropic.py:31
def _anthropic_stop_reason(raw, *, has_tool_calls) -> CompletionStopReason:
    normalized = str(raw or "").casefold()
    ...
    if normalized == "tool_use" or has_tool_calls:
        return "tool_use"
    if normalized in {"", "end_turn", "stop_sequence", "stop"}:   # ← None 落在这里
        return "stop"
```

`_openai_stop_reason` 同构。唯一的兜底是「文本与调用**都**为空才报错」。

**触发条件不是理论上的**：反向代理空闲超时、LB 的 60 秒上限、公司网关的响应体上限，
都会在一个 SSE 事件边界上**干净地**关闭连接。这种关闭下 `aiter_lines()` 正常结束、
不抛异常（只有在 chunked 分帧被破坏时 httpx 才抛 `RemoteProtocolError`）。于是：

1. **半截答案判成功**：正文写到一半连接断掉 → `stop_reason="stop"` → 落 checkpoint、
   run 标记 done、界面上没有任何迹象。
2. **残缺工具批次照常执行**：模型本轮发了 5 个 `tool_use`，只收到前 3 个 →
   `has_tool_calls=True` → `"tool_use"` → 执行 3 个，模型原本的计划是 5 步。

> **这是 §12.1/P7 那个洞低一层的形态。** P7 封的是 provider **自报**的 `length`；
> 这里是 provider **没来得及报**。同一句话适用：沉默的那一层仍然是 provider adapter。

Pi 踩过这个坑的痕迹在 `packages/ai/src/utils/retry.ts` 的可重试模式表里：

```
"ended without", "stream ended before message_stop",
"stream ended before a terminal response event",
"socket hang up", "socket connection was closed",
"websocket.?closed", "reset before headers",
```

**动作（小）**：三只 adapter 各记一个 `terminal_seen` 布尔，流结束时未见终止事件即抛
`ProviderRetryableError`，直接复用 §13.1 已有的同 endpoint 有界退避；重试耗尽再交给
`ModelTurnResult` 判失败。不要新增终态——这是传输故障，不是模型行为。

### 14.2 模型调用没有 attempt 记录，恢复即重放付费调用（严重）

`reap_expired_runs`（`runstore/runs.py:338`）会把租约过期的 **cowork** run 从最近
checkpoint 重投，上限 `max_recovery=3`。工具侧有 `tool_invocations` 租约保护；
**模型侧没有任何「我已经发出去一次」的痕迹**。

`_request_model_decision`（`cowork/runtime.py:3175`）拿到 completion 后要走完
usage 记账、文本工具调用抢救、`_record_completion_identity`，才由 `decide()` 交给
`_checkpoint`。这段窗口里进程死掉，恢复就是重新发一次：钱花两次，而且第二次的答案
可能与第一次不同。

`worker/maintenance.py:35` 的 docstring 自己写着：

> 普通回答不自动重跑: 一次已经发出去的模型调用是否计费无法确认, 静默重放等于重复计费。

——但这条政策写给的是**已退役的 answer 工作流**；cowork 走的恰恰是被自动重投的那条路。
政策和实现之间隔着一次工作流退役。

Pi 的对应物是 `step_attempt`（`harness/session/types.ts`）：

```ts
type StepAttemptRecord = RecordBase & {
    type: "step_attempt";
    runId: string;
    step: "assistant" | "compaction" | "branch_summary";
    attempt: number;              // 连续，reducer 校验
    resultEntryId: string;        // 请求发出前就预分配
}
```

**在请求之前落盘**，结果 entry 的 id 预先定好。`reducer.ts` 因此能区分「attempt 开了
但没有结果」，并对 `non_consecutive_attempt` 具名拒绝而不是猜。

**动作（中）**：turn 开始前往已有的 `session_entries` 写一条 attempt 记录（iteration +
预分配的 assistant message record_id）；恢复时发现开着的 attempt，按已有的
`InvocationOutcomeUnknownError` 哲学处理——计入 attempt 上限或显式提示，不静默重发。

### 14.3 §13.4 只做了 Pi 会话模型的一半

Pi 拆的是**两种**记录：

- **Entry**：模型看得见的东西，append-only，`parentId` 串成树
- **LaneRecord**：harness 做过什么的**意图日志**（`operation_started` / `step_attempt` /
  `tool_started` / `queue_enqueued` / `write_deferred` / `abort_requested`）

`reducer.ts` 把 records 回放成 `LaneState`，遇 12 类具名 `RecordLogCorruption` 之一就
拒绝恢复。**状态是推导出来的，不是存下来的。**

§13.4 加的 `session_entries` 是 Entry 那一半：时间线可读了、能 fork 了。恢复仍然读
`CoworkCheckpointState` 整份快照。所以现在的形状是「时间线可读、分支可 fork，但恢复
仍靠快照」，而 §14.2 修不彻底正是因为没有 record 层——attempt 只能寄生在 entry 里。

顺带一条 Pi 独有的机制：`ProvisionedEntry` 在意图记录里就带着完整 payload 和 id，
写入后 reducer 用 `matchesProvisionedEntry` 校验「实际写下的和当初打算写的是不是同一个」，
不一致就 `provisioned_entry_mismatch`。没有 record 层，这条无从谈起。

> **判断：这是当前最大的结构性分叉，但可以合理地不做完。** 快照恢复对单用户产品够用。
> 要做的话最小切法是只补 `step_attempt` 一类 record（`tool_started` 的等价物
> `tool_invocations` 已经有了），不引入 lane / navigation / operation 全套。

### 14.4 其余发现

| | 发现 | 位置 | 代价 |
|---|---|---|---|
| A | **steering 落进 canonical 时丢来源**：变成裸 `role:"user"`，代码注释自认「no provenance column」，于是 `semantic_review_user_text_source` 被打成 `"unknown"`——**一次 steering 就让语义审批失去授权依据**。`runtime_directive` 这个机制已经有了，没用上 | `cowork/runtime.py:3153` | 极小 |
| B | **JSONL 读取对任何损坏行 `continue`**。注释的安全论证只覆盖「最后一行」，代码却对所有行生效；而且 except 里还捞了 `KeyError/TypeError/ValueError`——**字段改名会静默丢历史消息**而不是报错。Pi 只修最后一行的语法错（原子重写有效前缀），中间行损坏直接 `invalidFile` | `cowork_store/jsonl.py:127` | 极小 |
| C | **只有 steering 一条队列**。Pi 有 steer / followUp / nextRun 三条，语义分别是「立刻插进当前 run」/「run 本来要结束时再续一轮」/「留给下一个 run」，且都是 durable record，带 `queue_cancelled` 与 `cancelQueued`。`agent_core/loop.py` 的 `get_follow_up_messages` 钩子存在但没接线 | — | 中 |
| D | **`AgentLoopConfig` 八条缝只接了一条**：`runtime.py:5063` 的唯一生产调用点只传 `get_steering_messages`，其余策略都挂在 `_CoworkExecution` 内部的 `CoworkHookBus` 上。两套 hook 概念并存，框架层那套是装饰，`memory_run` / `subagent` / `skill_distillation_run` 复用不到 | — | 中 |
| E | **压缩没有 span**。压缩本身是模型调用，「这个 run 为什么慢」里最可能的那一段没被计量。Pi 有 12 个 typed span，且 schema 里声明了**父子约束**（`parents: { kind:"spans", spans:[...] }`），含 `pi.harness.compaction` / `hook` / `sleep` / `session.write`；我们有 3 个 | `agent_core/compaction.py` | 小 |
| F | **旁路调用不挂账**：`conversation_titles` / `memory_extraction` / `skills.distillation` 直接拿 `ModelGateway` 而非 `BudgetedGateway`。不必改预算语义（它们确实不属于 run），但 Pi 有 `UsageRecord{cause:"hook"\|"adjustment"}` + `recordUsage()` 把这类调用显式归回会话账；否则「一次对话到底花了多少」永远缺一块 | 各 sidecar | 小 |
| G | **没有手动驱动**。Pi 的 `AgentLane` 声明了 `peekAction()` / `executeAction()` / `runToCompletion()` / `drive:"manual"` 与 `ActionInfo` 封闭 union（`append_entry` / `stream_assistant` / `execute_tool` / `hook` / `sleep` / `fetch_deferred`…），含义是**循环每一步都是从持久状态推导出的一个具名 action**，可外部单步、可断言「下一步应该是什么」——[ADR-0001](adr/0001-显式状态与可恢复Agent循环.md) 的完全形态。⚠️ **Pi 这部分现在全是 `HarnessNotImplemented`**，是设计方向不是既成事实，别写成「落后」 | `agent_core/loop.py` | 大 |

### 14.5 Pi 落后的部分，第六轮复核仍成立

§10「不要动的三块」全部复核有效。新增一条可写进面试话术的：

**重试分类我们比 Pi 强。** Pi 的 `utils/retry.ts` 是**拿正则匹错误文本**
（`"429"|"overloaded"|"socket hang up"|"getaddrinfo"`…）；我们是 adapter 按 HTTP status
归一成 `ProviderRetryableError` / `ProviderRateLimitError` 类型层级，配额耗尽的 429 不会
被误判成瞬时限流。

但那张正则表是**经验清单**，里面的 premature stream end / websocket close / `getaddrinfo`
恰好是我们没覆盖的失败面——即 §14.1。**分类机制更强，覆盖的失败面更窄**，这两件事同时成立。

其余复核结论未变：事件系统（Pi 的 `harness/events.ts` 只有 `run_start`/`run_end` 两个成员
且纯内存，vs 我们 44 成员封闭 union + SSE 持久化 + `Last-Event-ID`）、`tool_invocations`
幂等租约与 `outcome_unknown`、前缀缓存导向的上下文装配、三档路由与费用闸门（微美元整数）、
团队/子 Agent（`teams.py` + board，Pi 的 lane 尚未实现）。

Pi 结构性领先的是：provider 覆盖面（47 个 provider + OAuth / Bedrock / Vertex / Copilot）、
session 存储可插拔（JSONL + sqlite-node 后端 + conformance 套件 + torn-tail 原子修复）、
record log / reducer、typed span 树。

### 14.6 建议顺序

1. **§14.1**（三只 adapter 加 `terminal_seen`）—— 唯一一条既严重又便宜的
2. **§14.4 A、B** —— 两处极小改动，都是「已有机制没用上」，且 B 是约束 10 哲学的直接违反
3. **§14.2**（`step_attempt` 记录）—— 严重但要中等改动，做完顺带把 §14.3 的最小切法落地
4. 其余按代价排队；**G 不建议现在做**，Pi 自己都还没实现

### 14.7 核验与收敛结果

本轮没有把对比文字直接当规格照抄。结论分三类：六项真实问题已修，两项结构性差异确认但
不在这一轮硬塞半套实现，一项不是现成功能差距。

#### 已修复

1. **传输终止证明**：OpenAI-compatible 的普通流和 tool stream 必须看到 `[DONE]`，
   Anthropic tool stream 必须看到 `message_stop`，Gemini 完整响应必须有非空
   `finishReason`；否则统一抛 `ProviderRetryableError`，不再进入 stop-reason 的成功默认值。
   在尚未向 UI 发出 delta 时，`ModelGateway` 会在同一 endpoint 退避重试；如果正文已经
   发出，则明确失败且禁止跨档拼接，避免把两次生成接成一条答案。
2. **主模型调用意图日志**：SQLite schema v24 新增独立的 append-only
   `session_records`，当前封闭 record 类型为 `step_attempt`。请求前先落 `started`，预分配
   `result_checkpoint_id`；收到结果后落 `completed` / `failed`。恢复遇到 completed 会复用
   原 completion 和 usage，不再次请求 provider；遇到 open attempt 会以
   `ModelInvocationOutcomeUnknownError` 拒绝自动重放，租约 reaper 也会把 run 明确置失败。
3. **最小 record reducer**：`reduce_model_step_attempts()` 拒绝重复 record/seq、跨 run、
   非连续 attempt、多个 open attempt、无 start 的终态、终态身份不一致、终态后继续写、
   phase/result 不匹配等矛盾状态，并暴露稳定 corruption reason。恢复仍以 checkpoint 为主，
   没有把 Pi 的整套 LaneState 搬进来。
4. **steering provenance**：队列记录新增封闭 `source`（`local_owner` /
   `external_inbound` / `runtime` / `unknown`）。canonical user message 保留来源；runtime wake
   转成带 source 的 `runtime_directive`。语义审批只聚合 `local_owner` 原文，外部或未知来源
   fail-closed，不会因一条本地 steering 把授权证据永久抹掉。
5. **JSONL 损坏策略**：只容忍最后一行、无换行且 JSON 语法未完成的 torn tail。中间行或
   已换行的语法错、缺字段、非法 UUID、非法 role/status/seq、非对象数组字段均抛
   `JsonlConversationCorruptionError(path, line_no, reason)`，不再 `continue` 丢历史。
6. **可观测性与费用归因**：新增闭集 `agent.compaction` span，记录 forced/changed/mode、
   归档消息数、before/after token 和 trigger source；旧 SQLite span CHECK 会原地迁移。
   `AuditRecord` 与 `llm_calls` 新增 `cause`，主决策为 `primary`、压缩为 `compaction`、标题/
   记忆/Skill 蒸馏/语义审批为 `hook`。这些 sidecar 原本已有 gateway audit 与 run_id 传递，
   缺的是明确 cause，不是“完全没有挂账”。

#### 确认存在，但本轮不做半套实现

- **durable followUp / nextRun 队列**：当前产品语义是 active run 收 steering、idle conversation
  新建 run；通用 `run_tool_loop` 虽有 outer follow-up 回调，但没有产品级 durable queue。
  正确实现必须把“终态提交”和“确认无 pending follow-up”放进同一个原子边界，否则会出现
  终态检查后、run 落 done 前入队而永久搁置的竞态。这不是本轮三处“默认成功”缺陷，留作
  独立存储/API 设计。
- **两层 hook 的统一**：`AgentLoopConfig` 的生产调用确实只使用 steering；但
  `CoworkHookBus` 不是未接线，它已经直接承载 context transform、provider request、
  compaction、八条 tool gate 与 after-tool 投影，并支持 worker configurator。两者处理的
  value type 和时机不同，不能靠传七个 no-op 回调冒充统一。后续若统一，应先把 Cowork
  decision 拆成可持久化的 prepare/dispatch/materialize action，再收敛接口。

#### 不作为当前差距

- **manual drive**：Pi 的 `peekAction/executeAction` 当前仍是 `HarnessNotImplemented`。
  它是 ADR-0001 可继续演进的方向，不是对方已经交付而 WorkPilot 缺失的能力。

本轮专项回归覆盖 provider 流、gateway retry、record reducer/recovery、SQLite store、
steering/semantic approval、compaction、telemetry 与 sidecar cause。完整后端 **1312 passed**；
产品源码 **233 files** strict mypy、后端全量 Ruff 与 8 条 import-linter 契约全部通过。

### 14.8 后续结构落地（接续 §14.7）

§14.7 中暂缓的 durable queue 与 loop/hook 收敛已经按“先持久化边界、后统一生命周期”的
顺序完成；以下结论取代该节的“本轮不做”状态，但保留原文作为决策过程记录。

1. **崩溃窗口已形成回归矩阵**：覆盖 attempt `started` 前、`started` 后请求前、请求完成后
   结果落盘前、`completed` 后 checkpoint 前四个窗口。测试断言 open attempt 明确进入
   `outcome_unknown`、completed completion 可恢复复用、provider 不被重复调用，以及未见终止
   事件的 partial SSE 永远不能落成功 checkpoint。forced-final 进入付费调用前也先 checkpoint
   stalled 状态，恢复时走同一 durable attempt，而不是回到普通决策路径重放。
2. **三类消息采用持久化有序交付**：`steer | follow_up | next_run` 是封闭类型，支持 enqueue、
   cancel、claim/consume、来源 provenance 与幂等 `source_wake_id`。run 结束采用“先 seal、在同一
   事务领取终态边界消息”的协议；边界之后到达的 steer/follow-up 自动降为 next-run。FIFO、
   取消头消息后提升下一条、终态竞态、崩溃恢复和重复 dispatcher tick 均有测试。完整设计见
   [ADR-0018](adr/0018-Agent消息队列采用持久化有序交付.md)。
3. **record 层扩至最小可审计闭集**：当前 SQLite schema 为 **v26**，`session_records` 包含
   `step_attempt`、`queue_event`、`abort_requested` 与 `harness_action`。模型 step 覆盖
   `assistant | compaction | forced_final`；队列 enqueue/consume/cancel 与相应状态变更在同一
   事务，next-run consume 记录实际 `launched_run_id`。仍不引入完整 LaneState reducer，恢复
   主体继续使用 checkpoint。
4. **循环显式拆成四类 action**：通用 loop 现在以 `prepare → dispatch → materialize → execute`
   驱动，每个 action 都发出 started/completed/failed，并可写入 durable `harness_action`。
   `AgentLoopHookRegistry` 以稳定 id 注册 steering、context transform、model conversion、stop、
   next-turn、follow-up、before/after-tool 与 action observer；Cowork 生产路径使用同一 registry，
   不再靠七个 no-op 回调假装统一。provider/compaction/tool-gate 等领域 hook 保留其强类型值。
5. **接口与事件已闭环**：新增 queued-message enqueue/cancel API、持久化
   `queue.message.queued/applied/cancelled` 事件、worker/local-runtime next-run dispatcher，前端
   protocol 与 API client 同步支持。local-owner 来源在派生 run 中继续保持，未知/外部来源仍
   对语义审批 fail-closed。

最终验证：后端完整测试 **1329 passed**，产品源码 **233 files** strict mypy、后端全量 Ruff、
8 条 import-linter 契约、前端 TypeScript typecheck 与本次修改文件的 ESLint 均通过。
