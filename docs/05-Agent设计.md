# 05 · Agent 设计

**核心设计**：状态机而非调用栈。

> 借鉴 LangGraph — Agent 状态是显式的、可 JSON 序列化的 dict，每个节点是 `state → state` 的纯函数。
> 于是「断点续跑」「时间旅行调试」「人工中断」不是额外功能，而是架构的自然结果。

这条决定必须在第一行代码前定下来，后期改不动（CLAUDE.md 约束 2）。

> ## ⚠️ 2026-08-22：固定工作流已退役，赌注反过来了
>
> 本文原来的核心论点是「先做固定流程，把 HITL、幂等、断点续跑这些**可靠性机制验证正确**，
> 通用 Agent 放 Backlog」。**赌注赢了一半：可靠性机制确实被验证正确了，然后固定流程本身被
> 扔掉了。**
>
> `literature_review` 与 `answer` 两个 workflow_type 已退役（`WorkflowType` 里保留取值只为
> 读得出历史行），`app/rag/review/` 已删空。现在只剩 `cowork` 一条：一个两节点的
> `decide ⇄ execute_tools` 循环，模型自己决定下一步、自己维护任务清单（`todo_write`）。
>
> **为什么**：真实使用里，预先分解好的步骤表几乎每次都不是用户当下想要的那几步。固定流程
> 关掉的不只是 planner 的不确定性，也关掉了它的用处。**这是一个值得写进博客的负结果**——
> 但要说清楚它否定的是「预先分解」，不是「可靠性机制」：幂等、checkpoint、预算熔断、HITL
> 一个都没退役，它们现在服务的是循环而不是流程。
>
> **本文仍然有效的部分**：§1 状态定义、§2 的预算熔断四条口径、§3 工具规范与面向模型的
> 错误信息、§4 三层记忆（其中「不覆盖只失效」已成为唯一实现）、§5 可恢复性与幂等、
> §7 指标。**已作废的**：§2.1 固定图、§2.2「Backlog 蓝图」那张 planner/executor/reflector
> 图——它既不是现状也不再是目标。

---

## 1. 状态定义

```python
class PlanStepState(TypedDict):
    id: str
    idx: int
    description: str             # 人类可读的步骤描述，直接渲染到前端时间线
    tool: str | None
    depends_on: list[int]
    status: Literal["pending", "running", "done", "failed", "skipped"]

class BudgetState(TypedDict):
    max_tokens: int; used_tokens: int
    max_calls: int;  used_calls: int
    max_wall_ms: int; started_at_ms: int

class InterruptState(TypedDict):
    kind: Literal["write_confirm"]
    payload: dict                # 展示给用户的内容
    resume_token: str

class AgentState(TypedDict):
    schema_version: Literal["literature-review.v1"]
    run_id: str
    conversation_id: str
    goal: str
    document_ids: list[str]
    plan: list[PlanStepState]
    cursor: int                  # 当前执行到第几步
    documents: list[ReviewDocument]
    cards: list[ReviewCard]
    groups: list[ReviewGroup]
    comparison: str
    draft: str
    output_path: str | None
    artifacts: dict[str, str]
    budget: BudgetState
    interrupt: InterruptState | None
    status: Literal["executing", "waiting_human", "done", "failed",
                    "cancelled", "budget_exceeded"]
    error: str | None
```

**禁止塞进 state 的东西**：数据库连接、HTTP 客户端、闭包、模型实例。
一旦不可序列化，checkpoint 就废了，整个恢复能力随之崩塌。

当前固定图把受预算约束的综述草稿内联进 checkpoint，确保恢复与 HITL 预览读取同一份内容；
后续若产物规模超过单篇 Markdown 的边界，再迁移为对象引用，不能只存临时文件路径。

---

## 2. 图结构

### 2.1 当前图：两个节点

```text
        ┌──────────────────────────────────────────┐
START ─►│ decide          [会话选定的档位]           │
        │  装配 outbound 视图 → 模型原生 tool-calling│◄──┐
        └───────────────┬──────────────────────────┘   │
                        │ 有 tool_calls?                │
              ┌─────────┴─────────┐                     │
              │ 否                │ 是                  │
              ▼                   ▼                     │
             END          ┌───────────────────┐         │
                          │ execute_tools     │         │
                          │  capability 校验   │         │
                          │  一次性 call-id 审批│─────────┘
                          │  幂等租约 → 执行   │
                          │  checkpoint + 事件 │
                          └───────────────────┘
                                  │
                    ├─ 需要人 → waiting_human（进 Inbox）
                    ├─ 需要等时间 → sleeping（让出 worker）
                    └─ 预算超限 → budget_exceeded → END
```

**计划是模型声明的，不是图结构。** 用户可选计划模式：计划阶段只下发只读与交互工具，
`propose_plan` 提交方案后暂停；批准是 checkpoint 里 `mode` 的翻转，不是 prompt 约定——
未批准前写工具既不下发、执行边界也拒绝。批准后步骤变成任务清单。

`agent_plan_steps` 与 `todo_write` 的任务清单并存不互相替代：前者是 runtime 从 tool call
派生的**事后日志**，后者是模型主动声明的计划。

### 2.2 ~~通用 Agent 蓝图（Backlog，当前未实现）~~ → 已作废

下面这张图是当时设想的"通用 Agent"目标态。**它没有实现，而且已经不再是目标**：
实际落地的是上面那个两节点循环，planner / reflector 两层都没有做。

保留它是因为对比本身有信息量——**这三层里，只有 executor 那一层活下来了**。
reflection 的位置被"空转熔断"取代（按调用签名计数，同一签名超 3 次不再执行），
那是一个便宜得多也可靠得多的机制：它不需要再问一次模型。

```
                    ┌─────────┐
        START ─────►│ planner │  [重推理档 DeepSeek-V4-Flash]
                    └────┬────┘  任务分解 → 带依赖的步骤列表
                         │
                    步数 > 3 ?
                    ├─ 是 ─► interrupt(plan_approval) ──► 等用户确认
                    └─ 否 ─┐                                   │
                           ▼◄──────────────────────────────────┘
                    ┌──────────┐
              ┌────►│ executor │  [主力档] 选工具 + 生成参数
              │     └────┬─────┘  → 校验 → 执行 → 写 checkpoint
              │          │
              │     写/外发操作? ─► interrupt(write_confirm)
              │          │
              │          ▼
              │     ┌───────────┐
              │     │ reflector │  [重推理档] 结果自检
              │     └─────┬─────┘
              │           │
              │      ┌────┴─────┬──────────┬─────────────┐
              │      ▼          ▼          ▼             ▼
              └── 继续下一步   重试本步   重规划       完成/失败
                              (≤N次)   (≤M次)          │
                                                        ▼
                                                       END

  ★ 每个节点执行前后都做预算检查，任一超限 → status=budget_exceeded → END
```

（上图已作废，见 §2.2 开头。下面的预算口径仍然有效并且正在用。）

### 预算熔断的四条口径（约束 5）

实现在 `app/agent/budget.py`。三个维度的判定强度不同，这是刻意的：

| 维度 | 判定 | 会不会超出上限 |
|---|---|---|
| 调用次数 | 调用前精确判定 | 不会 |
| 执行墙钟 | 节点入口 + 每次调用前判定 | 不会 |
| token | 调用前按「输入估算 + `max_tokens`」最坏情况预留，调用后按实际结算 | 不会 |

1. **检查点在两处，不是一处。** 只在模型调用前查会漏掉两种失控形态：整节点不产生
   模型调用但长时间执行，以及抽卡这类**按文档数循环**的节点。所以节点入口也查墙钟。
2. **墙钟不计 `waiting_human`。** 累计的是"执行分段"时长，人工确认的思考时间与两次
   worker 之间的空档都不落在任何分段内。这一维防的是失控执行，把人的犹豫算进去只会
   让正常的 HITL 流程被误杀。代价是 `used_wall_ms` 只存在 checkpoint 里
   （run 行只有 token 与调用数），恢复执行时必须显式接管。
3. **失败也记账，除非能证明没发出去。** 与模型网关的费用记账同口径：只有
   `ProviderNotDispatchedError` 免记，读超时这类"可能已经打到模型上"的失败按预留量
   保守记账。少记会让熔断失效，多记只是让这个 run 早一点停。
4. **熔断不可重试。** worker 对普通失败有三次重试，但重跑同一节点只会继续烧同一份预算，
   重试三次等于把超额放大三倍。因此熔断在图内部落 `budget_exceeded` 终态后**正常返回**
   而不是抛异常——异常会被重试循环接住。恢复执行时上限一律以 `agent_runs` 行为准，
   不从 checkpoint 抄回来，否则给某个 run 提额之后恢复会继续用旧上限。

### 何时不做 Reflection

反思不是免费的，它至少让成本翻倍，且有可能把对的答案改错。规则：

- 单步、确定性工具（如"查日程"）→ **跳过反思**，直接信任
- 生成类步骤、多源汇总步骤 → 反思
- 已重试 2 次仍失败 → 不再反思，直接上报用户

> 面试题"Reflection 什么时候是负收益"的答案就在这——
> 并且要有实测数据：开/关反思在任务成功率与成本上的对比。

---

## 3. 工具规范

> 借鉴 Cline / Roo Code — 开源 coding agent 的工具描述被真实用户捶打过，比框架文档有营养。

工具定义不只写"这个工具做什么"，还必须写**什么时候不要用它**和**失败样例**：

```python
@tool(
    name="search_knowledge",
    description="在用户的个人资料库中检索片段，返回带页码的原文摘录。",
    when_to_use="需要引用用户读过的论文、笔记、书籍、剪藏网页中的具体内容时。",
    when_not_to_use=(
        "1) 需要用户本人的偏好或身份信息时——用 recall_memory；\n"
        "2) 需要按时间/标签列出文档清单时——用 list_documents，本工具做的是内容检索；\n"
        "3) 通用常识（如「什么是梯度下降」）不需要检索，直接回答；\n"
        "4) 用户问的是资料库里没有的新论文——如实说没有，不要用常识编造。"
    ),
    examples=[
        {"args": {"query": "InfoNCE 负样本构造", "doc_type": "paper", "top_k": 5},
         "note": "带 doc_type 过滤能显著提高精度"},
    ],
    failure_examples=[
        {"args": {"query": "它的负样本怎么构造"},
         "why_bad": "含指代词，检索质量差。应先做指代消解再调用。"},
        {"args": {"query": "对比学习", "top_k": 50},
         "why_bad": "查询过宽 + top_k 过大，返回一堆弱相关内容淹没答案。应细化查询意图。"},
    ],
    timeout_s=10, retries=2, fallback="return_empty_with_note",
)
```

### 面向模型的错误信息（CLAUDE.md 约束 4）

工具失败时返回的是**给 LLM 的可执行修复指令**，不是 stack trace：

| ❌ 面向人 | ✅ 面向模型 |
|---|---|
| `ValidationError: field required` | `缺少参数 start_date，需要 YYYY-MM-DD 格式，例如 2026-08-01` |
| `HTTPError: 404 Not Found` | `文档 doc_123 不存在。请先用 list_documents 确认可用的 doc_id` |
| `TimeoutError` | `检索超时。请缩小查询范围或减少 top_k 后重试（当前 top_k=50）` |

**这是投入产出比最高的一条优化**，第一周就该做，成本近乎为零，
对工具调用成功率的提升在实验中单独量化。

### 首批工具（M1 交付 5 个）

| 工具 | 作用 | 是否需要 HITL |
|---|---|---|
| `search_knowledge` | 资料库内容检索 | 否 |
| `list_documents` | 按时间/标签/阅读状态列出文档 | 否 |
| `extract_card` | 单篇文档 → 结构化卡片（强 schema） | 否 |
| `compare_docs` | 多篇文档横向对比（综述任务的核心） | 否 |
| `write_note` | 写回笔记库（Obsidian / Markdown） | **是** |

M2 追加：`recall_memory`（检索长期记忆）、`query_entity_graph`（知识图谱关联查询）。

---

## 4. 三层记忆

| 层 | 存储 | 内容 | 写入时机 | 状态 |
|---|---|---|---|---|
| **工作记忆** | `conversations.summary` + 最近 N 条消息 | 当前会话上下文 | 超阈值时滚动压缩 | ✅ |
| **长期记忆** | `cowork_memories` 表 | 研究方向、阅读偏好、输出格式习惯、身份背景 | 每轮对话后异步抽取 | ✅ |
| **知识图谱** | `entities` + `relations` | 概念-论文-作者-项目的关系网 | 文档入库时抽取 | 🔭 **从未实现** |

> **2026-08-22**：原 RAG 的 `memories` 与 Cowork 的 `cowork_memories` 已合并成一张表
> （[03 §1.7](03-数据模型.md)）。作用域是 OpenWorker 的扁平 global / workspace /
> conversation，改写用本项目的时序有效性——两个参照物都没有历史，这里有。

工作记忆已采用“两层上下文”：未归档历史按完整 JSON 注入形式做 token 上界估算，达到
当前回答模型完整上下文窗口的 90% 时，把较早回合与旧 `summary` 合并并推进
`summary_upto`。当前 grounded/main 窗口为 102400，因此触发线是 92160 tokens；切换
回答档位后阈值随目标模型变化。系统尽量保留最近 4 个原文回合；若长回合仍会让压缩后
占用超过阈值，则继续减少原文回合，为摘要留出空间。读取侧不再固定截到 6 回合/
6000 字符，只保留 500 回合与 100000 字符的防御性上界，并同时服从模型 token 阈值。
摘要失败时回退原文裁剪，不阻断主回答。checkpoint 使用乐观更新，且不会越过同会话中
尚未结束的并发 run。

字符预算之外还有模型感知的 token 硬上限：路由表为每档记录真实部署窗口，调用前从
窗口中扣除输出预算与安全余量，再以保守 token 估算检查完整 messages。查询改写与摘要
会主动缩短历史以适配当前 32768-token 的 light；任何模型调用若仍然超窗，都不会发送到
provider，而是在首 token 前选择容量足够的 fallback，或明确失败。

> 个人产品里记忆不是锦上添花，而是**核心价值**。
> "它了解我"是个人助手相较通用聊天机器人唯一不可替代的地方——
> 所以记忆的质量必须像检索一样被测量（见 §7 与 A5 实验）。

### 长期记忆写入：四操作分类

> 借鉴 Mem0 的抽取-比对两阶段设计。

```
对话结束
  │
① [轻量档] 抽取候选事实 → ["在做 RAG 方向的毕设", "综述偏好按方法分类而非时间顺序"]
  │
② 把最近 N 条活跃记忆整批放进 prompt，让模型指名道姓选一条改
   （原为向量召回 top-5；pgvector 退役后 `recall_memories` 已无调用者。
    对个人记忆这个量级反而更准——语义最近邻常挑出"相似但说的是别的事"的那条）
  │
③ [轻量档] 判定操作：
  │   ADD    — 全新事实，直接插入
  │   UPDATE — 与旧事实矛盾 → 旧记录写 invalid_at + superseded_by，插入新记录
  │   DELETE — 用户明确否认 → 旧记录写 invalid_at
  │   NOOP   — 已存在等价事实，仅更新 access_count
  │
④ 写入（全流程走轻量档，成本可忽略）
```

### 冲突消解：不覆盖，只失效

> 借鉴 Graphiti 的时序知识图谱 — 事实带有效期窗口。

用户三个月前说"我在做多模态检索"，现在说"我转做 Agent 评测了"：
**不删除旧记录**，而是给它写 `invalid_at = now()` 和 `superseded_by`，再插入新记录。

这一招同时买到两个能力：

1. **冲突消解**：当前检索只取 `invalid_at IS NULL`，自然拿到最新的研究方向
2. **时效性问答**：能回答"我三个月前在关注什么"——按时间点检索历史有效的事实

> 简单覆盖式记忆做不到第 2 点，而个人知识助手恰恰最需要它：
> 一个人的兴趣是**演进**的，不是被替换的。

评测集的 `temporal` 类别专门验证第 2 点。

### 记忆注入策略

不是把所有记忆都塞进 prompt（会稀释注意力且涨成本）：
按 query 向量召回 top-5 相关记忆 + 始终注入的核心画像（不超过 3 条）。
记忆注入的收益需要实验验证——**开/关记忆对任务成功率的影响**，
如果没有可测量的增益，就要诚实地写进博客。

### 产品化：记忆可见可编辑

前端提供记忆管理页，用户能看到"AI 记住了我什么"并手动删改。
这是**产品思维的直接证据**，也是隐私层面的必要设计。

---

## 5. 可恢复性

| 场景 | 行为 |
|---|---|
| 进程重启 | 从 `agent_checkpoints` 读回最新 state，从 `cursor` 继续 |
| 用户关闭页面 | worker 不依附 HTTP 连接，任务继续执行并落库；重进时回放 `run_events` 渲染时间线 |
| 单步失败 | 面向模型的错误信息 → executor 重试（≤2 次）→ 仍失败则 reflector 重规划（≤2 次） |
| 工具超时 | 走 fallback 链，降级结果标记 `status=fallback`，在时间线上明示 |
| 预算耗尽 | `status=budget_exceeded`，展示已完成部分 + 剩余步骤，询问是否追加预算 |
| 用户中断 | `status=cancelled`，保留 checkpoint，支持后续恢复 |

### 5.1 幂等必须落在工具副作用边界 ★

**一个必须澄清的陷阱**：LangGraph 从 `interrupt()` 恢复时，
**会从所在节点的开头重新执行**，而不是从中断那一行继续。
节点内 `interrupt()` 之前的所有副作用都会重跑。

这意味着：`write_note` 若在人工确认前已写了一次，用户点"确认"后会**再写一次**——
HITL 保护机制本身制造了重复写入。

**checkpoint 恢复的是状态，不是副作用。** 靠 `AgentState` 的 `cursor` 或
任何步骤序号做幂等都不成立（[ADR-0007](adr/0007-agent幂等与事件溯源.md)）。

正确做法：有副作用的工具执行前先在 `tool_invocations` 抢占幂等键。

```
idempotency_key = hash(run_id, plan_step_id, tool_name, canonical(args))

INSERT ... ON CONFLICT (idempotency_key) DO NOTHING RETURNING ...
  ├ 抢到       → 执行工具 → UPDATE status='succeeded', result=...
  └ 已 succeeded → 直接返回存储的 result，不重复执行 ★
```

**key 里不含 `attempt_no`**：同一步骤的任何重放（节点重执行、进程重启、
用户重复点确认）都算出同一个 key。`succeeded` 直接复用；`in_flight`
且租约未过期视为仍在执行；`failed` 或租约已过期时，通过 CAS 抢占并递增
`retry_count` 后重试。真正需要重跑时参数会变，key 随之变化，允许执行。

该协议只保证 WorkPilot 数据库内的状态机一致，不能凭空把外部副作用变成
exactly-once。调用支持幂等键的下游时必须透传同一个 `idempotency_key`；
本地写文件使用“临时文件 + `fsync` + 原子重命名”，并把最终路径记录到
`effect_ref`。

协议细节见 [03 §6.3](03-数据模型.md)。

> 面试题"长任务执行到第 5 步失败，怎么不从头再来"的完整答案：
> checkpoint 恢复状态 + `tool_invocations` 保证副作用不重放 +
> `agent_attempts` 记录每次尝试用于归因。
> **三者缺一不可**——只说 checkpoint 是答不完整的。

---

## 6. 计划外置

> 借鉴 deepagents — 把 TODO list 作为 agent 的显式外部状态，而不是藏在 context 里。

`plan` 存在 state 和数据库里，而非仅存于对话历史。好处：

- 上下文压缩不会丢失计划（长任务最常见的失控原因）
- 前端可以直接渲染成进度时间线
- 用户能在执行中途看到并修改剩余步骤

---

## 7. Agent 层指标

| 指标 | 定义 |
|---|---|
| `task_success_rate` | 端到端达成目标的比例（Judge + 规则双轨判定） |
| `step_efficiency` | 实际步数 / 标注的最优步数 |
| `tool_selection_accuracy` | 选对工具的比例（对比 `gold_tools`） |
| `tool_error_rate` | 工具调用失败率（区分参数错误 vs 执行错误） |
| `recovery_rate` | 失败步骤中被自动恢复的比例 ← **可靠性工程的核心证据** |
| `interrupt_precision` | 该请求确认时确实请求了、不该请求时不打扰 |
| `cost_per_task` / `p95_task_latency` | 成本与性能 |

---

## 8. Agent 实验路线图

| # | 实验 | 变量 |
|---|---|---|
| A1 | 面向模型的错误信息 | 有/无，测 `tool_error_rate` 与 `recovery_rate` |
| A2 | 工具描述含失败样例 | 有/无 `when_not_to_use` + `failure_examples`，测选择准确率 |
| A3 | Reflection 开关 | 全开 / 全关 / 按步骤类型选择性开，测成功率 vs 成本 |
| A4 | 计划外置 | plan 在 state vs 仅在对话历史，测长任务的计划漂移率 |
| A5 | 记忆注入 | 开/关长期记忆，测任务成功率与用户满意度 |
| A6 | Planner 档位降级 | 重推理档 vs 主力档做规划，测计划质量 vs 成本 |
