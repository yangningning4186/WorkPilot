# 内容契约

## Office Brief

```markdown
# OFFICE BRIEF
- job: <读者使用产物完成什么>
- audience_and_scene: <受众 / 使用场合>
- deliverable: <格式 / 数量 / 语言 / 长度>
- content_boundary:
  - must_include: [...]
  - must_not_include: [...]
  - time_scope: ...
  - unit_scope: ...
- fidelity: verbatim | synthesize | create
- success_checks: [...]
- assumptions: [...]
- open_questions: [...]
```

Brief 是工作契约，不是给用户看的长篇需求复述。只保留会影响结果的项目。

## Source map

每条材料只设一个主类型，避免同一事实被重复消费：

```markdown
| id | type | content | source/locator | status | target_use |
|---|---|---|---|---|---|
| S01 | fact | 收入 1,240 万元 | performance.md#收入 | confirmed | KPI/结论 |
| S02 | decision | 仅华东试点 | notes.md#已决 | confirmed | 范围约束 |
| S03 | open | 隐私评审日期 | notes.md#未决 | unresolved | 风险，不写成结论 |
```

`type` 使用 fact / metric / decision / action / opinion / quote / visual / open。冲突事实分别保留并标注
来源，不平均、不挑一个看起来顺眼的值。

## Content map

每个可见单元都回答五个问题：

1. **purpose**：这一节/页/sheet 为什么存在；
2. **message**：读者看完只应记住什么；
3. **support**：哪些事实、数据、例子或推理支撑它；
4. **action**：它改变什么判断、行动或下一步；
5. **evidence**：来源在哪里，哪些仍未确认。

纯转场可以没有 support，但必须明确为转场；普通内容单元不能只有标题。

## 格式映射

### DOCX / PDF / HTML

```markdown
section: <功能性或结论式标题>
purpose: ...
claims: [...]
blocks: paragraph | bullets | table | quote | callout | image
evidence_ids: [...]
```

段落承担论证，列表承担并列项，表格承担对齐关系，callout 承担结论/风险。不要用空格、短横线或
一堆加粗文本模拟原生结构。

### PPTX

```markdown
slide: s01
message: <结论句>
support: <本页证据>
visual_role: anchor | evidence | atmosphere | none
visual: <图表/图片/流程/矩阵/数字；它证明什么>
density: focus | standard | dense
content_units: <指标/条目/节点数量>
whitespace_intent: <为何需要留白；普通页不得写“极简”敷衍>
evidence_ids: [...]
```

`focus` 只给封面、章节、真正的峰值结论或强视觉页；普通 supporting 页使用 `standard`，必须有
完整主张和支撑。不能用装饰图抵扣内容缺口。

### XLSX

```markdown
sheet: Summary
purpose: 决策概览
regions:
  - type: input | detail | calculation | summary | chart | notes
    anchor: ...
    fields/formulas: ...
    unit/format: ...
review_points: <公式、范围、异常与重算检查>
```

先定区域锚点再写公式。可推导值必须是公式，Summary 必须引用明细/输入，不能复制一份硬编码结果。

## 密度不是字数

有效密度由“信息单元 + 视觉载体 + 阅读任务”决定：

- focus：一个强主张、关键数字或主视觉，留白有明确强调作用；
- standard：至少两个互补信息区，或一个证据型主视觉加解释；
- dense：多个指标/步骤/对比，但仍只有一个页面主张。

以下都算空洞：标题 + 空容器；大卡片里只有一行；连续多页只有一句泛泛表述；图片只是小装饰；
表格有样式却没有可用字段/公式；文档有很多章节但每节只有一句。
