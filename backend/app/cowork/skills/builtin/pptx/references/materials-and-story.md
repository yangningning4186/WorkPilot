# 材料、需求与演示故事线

这份参考维护 PPT 的对齐、材料解析、故事线、密度与设计契约。规划与生成属于同一个 `pptx` Skill；
用户只要规划时在本参考完成后停止。

## 1. 对齐门槛

按顺序从请求、上下文和附件推断，不重复询问已知项：

| 优先级 | 信息 | 决定什么 | 用户把选择交给你时的默认 |
|---|---|---|---|
| P0 | 主题、必须讲/不能讲的内容 | 能否开始 | 主题不能完全为空 |
| P1 | 核心目标与观众结果 | 主张与结尾 | 从使用场景推断并记入 assumptions |
| P2 | 受众与场合 | 深度、语气、密度 | 内部评审 / 熟悉业务的同事 |
| P3 | 页数或时长 | 取舍与节奏 | 快速分享 5–7；内部汇报 6–8；提案 8–12；培训 8–15 |
| P4 | 材料忠实度 | 能否重组、删减、补充 | 有材料适度提炼；无材料自由创作 |
| P5 | 模板/品牌/风格硬约束 | 视觉套件、颜色、字体 | 选最匹配场景的内置视觉套件 |

只问无法推断且会改变内容边界或结构的缺口，一轮最多 3 问。用户说“直接做”时采用合理默认；
结论、数据口径、对外敏感表达或品牌使用权未确认时列为 open question，不写成既成事实。

## 2. Brief 与 source map

简单任务可在上下文维护，复杂材料任务写入同一项目目录；不要为了流程感创建空文件。

```markdown
# PPT BRIEF
- job: <观众用这份演示理解、判断、决定或完成什么>
- audience_and_scene: <受众 / 场合>
- deliverable: <PPTX 或 planning-only / 页数 / 时长 / 语言>
- content_boundary: <必含 / 禁止 / 时间与单位口径>
- fidelity: verbatim | synthesize | create
- success_checks: [...]
- assumptions: [...]
- open_questions: [...]
```

材料只解析一次。每条记录一个主类型：fact / metric / decision / action / opinion / quote / visual / open。

```markdown
| id | type | content | source/locator | status | target_use |
|---|---|---|---|---|---|
| S01 | metric | 收入 1,240 万元，2026 H1 | report.pdf p.7 | confirmed | KPI/结论 |
| S02 | open | 隐私评审日期未说明 | meeting.md#风险 | unresolved | 风险页，不写成承诺 |
```

- 数字保留原值、单位、时间口径和来源；冲突项并列，不平均、不挑一个“顺眼”的值。
- 观点与引文保留说话者和语气，不能改写成客观事实。
- 决定与行动保留状态、负责人、日期和依赖；材料未说明就明确写未说明。
- 视觉素材记录绝对本地路径、实际内容、比例、清晰度、授权/来源与适合的视觉角色。
- 每个重要 claim 记录 evidence id；去重时保留信息最完整、来源最直接的一条。

## 3. Communication job 与叙事

先写一句：

> By the end, **[受众]** should **[理解/相信/选择/批准/完成什么]** because **[核心结论]**。

再选与任务匹配的叙事弧，而不是套固定目录：

- 背景 → 利害 → 证据 → 含义 → 行动；
- 问题 → 分析 → 答案；
- 问题 → 原因/选项 → 建议；
- 现状 → 变化 → 未来状态；
- 时间、流程、学习进阶或 claim → evidence → consequence。

Agenda 不是叙事。每一页回答前一页留下的问题或为下一页建立必要性。开场 1–2 页内建立问题、
承诺或结论；结尾必须解决开场，落到判断、决定、行动、应用或有价值的问题，不能突然停在细节或
泛泛的“谢谢”。

## 4. STORY 契约

```markdown
# STORY
## Meta
- Audience / Goal / Scene prototype / Audience outcome
- Deck thesis / Slide count / Language
- Assumptions / Open questions

## Slides
### 01 · <结论式标题>
- id: s01
- role: hero | supporting | transition
- rhythm: peak | valley
- function: opening | context | explanation | evidence | activity | debrief | decision | action | closing
- message: <观众只应记住的一句话>
- support: <事实、数字、例子或推理>
- audience_action: <看、比较、讨论、选择、练习或承诺；纯讲述可省略>
- layout_intent: <受支持 layout>
- visual_role: anchor | evidence | atmosphere | none
- visual: <它如何支撑 message>
- density: focus | standard | dense
- content_units: <可见条目/指标/节点数量>
- whitespace_intent: <仅 focus 页说明为何留白>
- anti_pattern: <最需避免的失败>
- evidence_ids: [...]
- notes: <来源、口径或讲者提示>
```

`message` 必须是主张而不是栏目名。“试点转化率 18%”不完整；“试点转化率 18%，已达到扩大灰度
门槛”才说明意义。每页一个 message；删掉后几乎不损失理解的页面应合并或删除。

## 5. 场景闭环

- 管理复盘：结论 → 偏差 → 原因/风险 → 行动与检查点。
- 方案/提案：问题 → 目标 → 方案 → 证据 → 风险 → 路线图/决策请求。
- 研究/分析：问题 → 方法/范围 → 发现 → 解释 → 局限 → 建议。
- 培训/分享：为什么 → 概念 → 示例 → 方法 → 练习/应用。
- 班会/课堂/工作坊：目标与引入 → 必要知识 → 示例/体验 → 完整互动 → 复盘 → 共同/个人行动 → 收束。

主持型演示至少一个完整互动页：可见问题、2–4 个步骤、时限、参与方式和复盘问题。百科式“起源、
习俗、诗词”不构成可主持流程，知识必须导向观察、比较、分享、创作或行动。

## 6. 密度与视觉角色

| 密度 | 适用 | 门槛 |
|---|---|---|
| focus | 封面、章节、峰值结论、关键数字、强视觉 | 一个强主张/主视觉；留白服务强调 |
| standard | 大多数 supporting 页 | 两个互补信息区，或证据型主视觉 + 解释 |
| dense | 数据、方案、对比、路线图 | 多个可扫单元，但仍只服务一个 message |

视觉层级：L1 是决定构图的图片、图表、数字、矩阵、时间线或关系结构；L2 是解释与对照；L3 是
来源、单位、caption。小图标、装饰线和角落插画只是 L3，不能抵扣普通页的 L1 或 support。

- `anchor` 建立场景/对象/核心概念，尺寸足以主导构图；
- `evidence` 证明趋势、状态或事实，可核验且标题写清含义；
- `atmosphere` 只建立情绪/品牌感，用于封面或转场，不能当证据；
- `none` 只用于强原生结构或真正 focus 文字页。

5 页以上 focus 通常不超过约三分之一。连续 3 页 focus、连续 3 页无证据型视觉、或连续 3 页同一
信息结构，都应回到故事线重排。

## 7. DESIGN 契约

```markdown
# DESIGN
## Visual kit
- visual_kit: <catalog id>
- reason: <受众/场合/内容匹配理由>
- explicit_theme_overrides: <仅用户品牌硬约束>

## Strategy
- layout_rules / image_strategy / chart_strategy / density_baseline

## Asset manifest
| id | source | local_path | actual_content | ratio | pages | visual_role | checked |

## Slide mapping
| id | function | role/rhythm | layout | primary_visual | visual_role | density | content_units | review_page | anti_pattern |
```

DESIGN 只写 `PresentationSpec` 和 PptxGenJS Renderer 能兑现的控制项，不承诺动画、复杂母版、任意
嵌套组件或像素级复刻。逐页映射要证明每个主张都有匹配的可见结构；`review_page=yes` 至少覆盖
封面、最密页、第一张证据/主视觉页、互动/决策页和结尾。

## 8. Planning-only 交付

用户明确不要文件时，交付 Brief/source map 摘要、STORY、视觉套件与 DESIGN 页面映射即可。不要
构造 Spec、不要试制、不要生成 PPTX；但仍应说明假设、未决项、来源边界和未来生成时的代表页。
