# 14 · 技能自进化与 MCP 集成

> **状态：客户端与人工生命周期已实现（2026-08-19）。** MCP client 的 stdio /
> Streamable HTTP、OAuth 凭据绑定、工具策展、目录哈希漂移保护、只读与逐次审批
> 副作用工具均已进入 Cowork；本地 Skill 已支持发现、渐进加载、安装、更新、启停、
> 删除、ZIP 导入和资源读取。MCP server 与 Skill 自动抽取/自动晋升仍是后续方向。

---

## 0. 定位:为什么这两件事属于本项目

**技能自进化 = 记忆体系的第四层。** [05 §4](05-Agent设计.md) 的三层记忆
(工作记忆 / 长期记忆 / 知识图谱)覆盖了情景记忆与语义记忆,
缺的是**程序性记忆**——"这类任务上次是怎么做成的"。
技能库补齐这一层,四层各司其职:

| 层 | 记的是什么 | 例子 |
|---|---|---|
| 工作记忆 | 当前对话 | — |
| 长期记忆 | 关于用户的事实 | "在做 RAG 方向毕设" |
| 知识图谱 | 资料之间的关系 | 概念—论文—作者 |
| **技能库(新)** | **任务是怎么做成的** | "对比类综述:先按方法轴抽卡,分组前先对齐术语" |

> 借鉴 Voyager 的 skill library 与 CODESKILL / MemEvolve 的经验蒸馏——
> 这批论文就在本项目语料库里,E7/P1 的 badcase 全出自它们。
> 差异点:上述工作都缺**晋升门禁**,坏技能直接进库。本项目的版本是
> **带评测门禁的自进化**,这正是全项目"可测量的质量"哲学在 Agent 层的延伸。

**MCP = 工具与生态的标准接口,双向。**
服务端方向,WorkPilot 把个人资料库暴露给外部 Agent(Claude Code / Desktop),
"不抢 Obsidian 阵地、做基础设施"的定位由此兑现到 Agent 生态;
客户端方向,通用 Agent 通过 MCP 消费外部工具,不必为每个数据源手写 connector。
对 [01 R8](01-总体规划.md)(个人项目 vs 企业岗位的叙事风险)这是直接弹药:
MCP 治理(工具策展、注入防护、幂等透传)恰是企业 Agent 平台的核心问题。

---

## 1. MCP 服务端:把资料库变成生态里的一等公民

### 1.1 工具面(v1 只读优先)

| 工具 | 说明 | 阶段 |
|---|---|---|
| `search_knowledge` | 混合检索,返回带 `block_id`/页码/quote 的片段 | v1 |
| `list_documents` | 按时间/标签/类型列文档 | v1 |
| `get_document_outline` | 标题层级树 + block 摘要 | v1 |
| `ask_grounded` | 完整溯源问答(消耗 LLM 预算) | v2 |
| `create_draft` | **只建草稿,不直接写** | v2 |

**引用元数据必须过界完整**(约束 3):MCP 返回结构里保留
`block_id / version_id / page_no / quote`,外部 Agent 也能给出可溯源引用——
这是本产品在生态里的差异化,不能在接口层丢掉。

### 1.2 写操作的 HITL 不跨界外包 ★

MCP 客户端那端没有本产品的确认界面,所以**外部 Agent 永远拿不到直接写权限**:
`create_draft` 只在 [13 §3](13-办公工作台与文档编辑.md) 的草稿箱里落一条,
确认发生在 WorkPilot 自己的界面里。HITL 的边界跟着产品走,不跟着调用方走。

### 1.3 工程形态

- 官方 Python SDK,streamable HTTP 挂在 FastAPI 下(`/mcp`,bearer key),
  另给 stdio 启动器供本机 Claude Code 用。
- **直接调 services 层,不经 HTTP 自环**;鉴权、限流、每日费用上限全部复用
  ([12](12-安全与部署.md)),`search_knowledge` 的 embed 成本照常入账。
- 这是全提案里最便宜的一块(约 2 天),且独立于其他一切——
  落地后作者在 Claude Code 里就能查自己的资料库,dogfooding 面直接翻倍。

---

## 2. MCP 客户端:外部工具进来,治理跟上

挂在通用 Agent(Backlog #2)的工具注册表上,**注册表先于 MCP 客户端存在**。

### 2.1 策展制而非直通制 ★

`config/mcp.yaml`(与 routing.yaml 同风格,凭据走 `${ENV}` 展开):

```yaml
servers:
  filesystem:
    transport: stdio
    command: ...
    tools:
      read_file:
        enabled: true
        side_effect: false
        data_scope: corpus_allowed   # 允许接收资料库内容
        when_to_use: "..."           # 人工策展,必填
        when_not_to_use: "..."       # 人工策展,必填
      write_file:
        enabled: false               # 未策展默认禁用
```

三条铁律:

1. **未策展的工具默认禁用。** A2 实验已证明工具描述质量直接决定选择准确率,
   第三方 server 自带的描述是别人写的,质量不受控;每个启用的工具必须补
   `when_to_use / when_not_to_use`(格式同 [05 §3](05-Agent设计.md))。
2. **未标注 `side_effect: false` 的一律按有副作用处理**:
   只有 `approval: always` 才允许启用，执行前进入 Unattended Inbox/HITL，随后
   进 `tool_invocations` 幂等协议(约束 9)。
   支持幂等键的下游透传同一个 key;不支持的只承诺 effectively-once。
3. **`data_scope` 管住语料外流**(约束 7 的延伸):资料库内容只允许流向
  `corpus_allowed` 的本地 server;默认 deny,远程 server 想拿语料需逐个显式放行。

### 2.2 安全:第三方工具输出是不可信输入

- **注入防护**:MCP 工具结果以定界标记包裹注入上下文,并立规则——
  **读过外部内容之后的副作用操作,无条件 HITL**,不管该工具本身是否只读。
  工具输出可以提供信息,不能授权动作。
- **投毒防护**:连接时对 server 的工具清单(名称 + schema + 描述)取哈希;
  哈希变化即禁用该 server 直到人工复审。上游偷改描述不能静默生效。
- **预算口径**:MCP 调用计入 `max_calls` 与墙钟;工具 schema 注入 prompt 的
  token 计入任务成本(工具越多 planner 越贵,这本身就是 M2 实验素材)。
- **错误翻译**(约束 4):MCP 错误码/异常一律翻译成面向模型的修复指令,
  原始 stack trace 不进上下文。

---

## 3. 技能自进化:带门禁的程序性记忆

### 3.1 技能的形状

```python
class Skill(TypedDict):
    id: str
    name: str                      # "对比类综述的抽卡顺序"
    trigger: str                   # 何时适用(嵌入检索 + planner 判定的依据)
    anti_trigger: str              # 何时不适用(与工具规范同纪律)
    procedure: str                 # 步骤草图:自然语言 + 工具序列
    tools: list[str]
    provenance: list[str]          # 来源 run_ids,可回放归因
    status: Literal["candidate", "active", "deprecated"]
    version: int                   # 时序有效性建模同 ADR-0005:
    superseded_by: str | None      # 不覆盖,只失效
    invalid_at: str | None
    stats: SkillStats              # uses / successes / last_used_at
```

存 `skills` 表,embedding 建独立部分索引(不与 chunk/memory 混索引)。

### 3.2 生命周期:抽取 → 合并 → 晋升 → 注入 → 退化

```
run 结束(status=done 且无用户否定信号)
  │
① [light] 蒸馏候选技能:从 plan + attempts + 产物反推"可复用的做法"
  │    排除:eval 标签的 run、触碰过 test 语料的 run ★(见 3.4)
  │
② 与既有技能向量召回 top-5 → 四操作判定(复用记忆管线的机制):
  │    ADD / UPDATE(旧版 invalid_at + superseded_by) / NOOP / DEPRECATE
  │
③ 晋升门禁:candidate → active
  │    v1:人工审批(/skills 页逐条过,单用户成本可忽略)
  │    v2:夜间回放门禁——候选技能在其来源任务 + 相邻 agent_task 样本上
  │        回放,成功率不低于无技能基线才晋升
  │
④ 注入:planner 检索 top-3 active 技能进 prompt(name+trigger+procedure);
  │    检索到的技能 id + 库快照哈希写入 AgentState(可序列化,约束 2)
  │
⑤ 退化检测:按技能统计成功率;跌破阈值或引发预算超限 → 自动降回 candidate
     并在 /skills 页标红。语料/工具变更是主要退化源(S3 实验)。
```

**v1 用人工晋升不是妥协,是设计**:自进化文献的普遍问题是坏技能污染库,
而单用户产品里人工审批的成本近乎零、还顺手产出了晋升判据的标注数据——
v2 的自动门禁就用这批数据校准。`/skills` 页与 `/memory` 页同构:
"AI 学会了什么"可见、可编辑、可禁用。

### 3.3 可复现性:技能库必须可冻结 ★

技能让 Agent 行为随时间漂移,这与评测的可复现性正面冲突。约束:

- 每个 run 记录 `skills_snapshot_hash`;
- `eval.run` 新增 `--skills=off|frozen:<hash>`,评测默认 **off**,
  技能实验(S 系列)显式冻结某个快照——与 `--no-fallback` 同理;
- 夜间门禁跑在 `--skills=off` 上:门禁衡量的是底座,不是技能增益。

### 3.4 评测污染是这里最大的暗坑 ★

技能从执行历史里学,而执行历史可能包含评测跑批——技能等于把 gold 答案
背下来再在评测里复述。三道防线:抽取阶段排除 eval 标签 run;
排除触碰过 test 集文档的 run;S 系列实验报告技能来源 run 与评测集的重叠审计
(类比 [G1](experiments/2026-08-17-G1-首份baseline快照与门禁点亮.md) 的隐私审计一节)。

---

## 4. 评测设计

### 指标

| 指标 | 定义 |
|---|---|
| `skill_reuse_rate` | 任务中检索并实际采用技能的比例 |
| `skill_hit_precision` | 注入的技能确实被 plan 采纳的比例(注入≠有用) |
| `task_success_delta` | 开/关技能库的成功率差(paired,同 A5 方法) |
| `step_efficiency_delta` | 技能是否真的减少步数与成本 |
| `pollution_incidents` | 坏技能导致的回归次数(门禁价值的直接证据) |
| `tool_selection_accuracy`(扩展) | 外部干扰工具存在时是否仍选对 |

### 实验路线(S/M 系列)

| # | 实验 | 变量 |
|---|---|---|
| M-1 | MCP 服务端一致性 | 同一查询走内部 API vs MCP,结果与引用逐字段一致 |
| M-2 | 干扰工具 | 注册 N 个无关外部工具,测选择准确率与成本变化 |
| M-3 | 描述策展价值 | server 原始描述 vs 人工策展描述(A2 的 MCP 复刻) |
| S-1 | 技能注入增益 | 开/关技能库,agent_task 成功率/步数/成本(paired) |
| S-2 | 晋升门禁价值 | 无门禁全量晋升 vs 人工门禁,污染率对比 |
| S-3 | 技能时效 | 工具/语料变更后旧技能的退化与检测延迟 |
| S-4 | 抽取档位 | light vs main 蒸馏的技能质量 |

> **诚实条款**:单用户的任务多样性有限,技能库可能长期只有十几条,
> S-1 可能测不出显著增益。负结果照写台账——
> "自进化在个人规模下的收益边界"本身就是有价值的结论([01 §3](01-总体规划.md) 的记忆条目同款立场)。

新 task_type:`skill_extract` / `skill_merge`,routing.yaml 登记 light 档,
走网关记账(约束 1)。

---

## 5. 排期与依赖

依赖关系决定顺序,不是偏好:

- **MCP 服务端**:零依赖,2 天,收益即时(Claude Code 里可查资料库)。
  建议排在 M2 收口后第一批。
- **MCP 客户端**:依赖通用 Agent 的工具注册表,3–4 天。
- **技能自进化**:依赖通用 Agent(要有真实执行轨迹可学)+
  记忆管线(复用四操作机制)+ MCP 客户端(工具面足够宽,技能才有内容),
  5–6 天。**它是整条 Agent 线的最后一块,不是第一块。**

```
M2 收口 → MCP-S(2d) → E1–E2 编辑 → 记忆(5d) → E3–E4
        → 通用 Agent(5–7d) → MCP-C(3–4d) → 技能自进化(5–6d) → 图谱/digest
```

| 阶段 | 验收 |
|---|---|
| MCP-S | Claude Code 连上并完成带引用检索;M-1 一致性通过;费用入账验证 |
| MCP-C | 策展制配置生效;未策展工具确实不可见;M-2/M-3 台账 |
| 技能 v1 | 抽取→人工晋升→注入→快照冻结全链路;`/skills` 页;S-1 paired 数据 |
| 技能 v2 | 回放式自动晋升门禁;S-2 门禁价值数据 |

---

## 6. 风险登记

| # | 风险 | 应对 |
|---|---|---|
| SM-R1 | **坏技能污染库,整体成功率反降** | 晋升门禁 + 退化自动降级 + `/skills` 可禁用;S-2 量化门禁价值 |
| SM-R2 | **技能记住了评测答案**(污染) | §3.4 三道防线;台账附重叠审计 |
| SM-R3 | 评测不可复现(技能漂移) | §3.3 快照冻结;门禁默认 `--skills=off` |
| SM-R4 | 第三方工具注入/投毒 | §2.2:输出不可授权动作、清单哈希锁定、外部内容后副作用强制 HITL |
| SM-R5 | 语料经 MCP 外流(违反约束 7) | `data_scope` 默认 deny;远程 server 逐个放行并留审计日志 |
| SM-R6 | MCP 规范/SDK 演进导致返工 | 锁 SDK 版本;服务端只用 tools 原语,暂不碰 resources/prompts/sampling |
| SM-R7 | 单人任务面窄,技能统计无力 | 诚实条款;把"收益边界"当结论而不是失败 |
| SM-R8 | 范围膨胀(两个新支柱同时铺开) | 只有 MCP-S 提前;其余严格按 §5 依赖链排队 |

---

## 7. 与蓝图的关系

定稿动作:[11 MVP 边界](11-MVP边界.md) Backlog 插入 MCP-S / MCP-C / 技能 v1 / 技能 v2
四条(解锁前提见 §5 依赖);[05 Agent 设计](05-Agent设计.md) §4 三层记忆表
扩为四层并链接本文;新增 `ADR-0009 技能晋升门禁与快照冻结`、
`ADR-0010 MCP 工具策展制`。M-2/M-3/S-1 至 S-4 并入实验路线图。
