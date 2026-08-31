# DocumentSpec 字段速查

`render_artifact` 的参数是 `{path, spec, skill_name, baseline_sha256?}`；`spec` 就是下面这个
对象。**未列出的字段一律被拒绝**（schema 是 `extra="forbid"`）。

## 顶层

| 字段 | 必填 | 说明 |
|---|---|---|
| `schema_version` | 是 | 固定 `1` |
| `artifact_type` | 是 | 固定 `"docx"` |
| `title` | 是 | ≤300 字符，渲染为 Title |
| `subtitle` / `author` | 否 | ≤500 / ≤200 |
| `purpose` / `audience` | 否 | 写给评审看的意图说明，不进正文 |
| `evidence_policy` | 否 | `none` / `optional`（默认）/ `required` |
| `claims` | 否 | ≤500 条，见下 |
| `sections` | 是 | 1–100 节 |

## sections[]

| 字段 | 必填 | 约束 |
|---|---|---|
| `id` | 是 | `[A-Za-z0-9._:-]+`，≤120 |
| `heading` | 是 | ≤300 |
| `level` | 否 | 1–3，默认 1。**第一节必须是 1，且不得跳级** |
| `blocks` | 否 | ≤500 个 block |

所有 section `id` 应唯一；所有作为 claim 靶子的 paragraph `id` 也应唯一。Schema v1 目前不会完整
拒绝重复 id，但重复会让证据绑定产生歧义，按 Skill 质量契约视为错误。

## blocks[]

`type` 决定哪些字段会被当前 Renderer 消费。Schema 只严格校验图片字段和表格形状，其他无效字段
有时会被接受但静默忽略，所以每个 block 只填写下表对应字段。

| type | 用哪些字段 |
|---|---|
| `paragraph` | `text`（≤20000）、`style`（可见差异仅 `lead`/`caption`）、可选 `id`（claim 靶子只能是它） |
| `bullets` | `items`（≤100 条，每条 ≤1000） |
| `table` | `headers`（≤10）+ `rows`（≤2000 行，每行列数必须等于列数），单元格可为 str/int/float/bool/null |
| `quote` | `text` |
| `callout` | `text`；`style` 可记录语义，但当前 DOCX Renderer 不区分 warning/positive 外观 |
| `image` | `image_path`（必填）、`image_alt`（**必填**，装饰图给 `""`）、`image_caption`、`image_width_inches`（1.0–6.5） |

`style` 取值：`normal` / `lead` / `caption` / `positive` / `warning`。

表格质量要求有非空 headers；Schema 虽允许只从 rows 推断列数，但没有表头的数据表可读性差。主体是
大量明细或超过约 50 行时优先交付 XLSX，DOCX 只保留摘要或关键行。

## claims[]（只在需要事实溯源时写）

```json
{"claim_id": "c1", "text": "Q2 收入增长 12%", "evidence_ids": ["ev-quarterly-1"],
 "target_type": "docx_paragraph", "target_id": "p-revenue"}
```

`target_id` 必须是某个 **paragraph** block 的 `id`。表格、列表、图片不能当靶子——
需要给表格找依据时，在表格前后放一个带 id 的 paragraph 说明数据来源。

## 最小可用示例

```json
{
  "schema_version": 1,
  "artifact_type": "docx",
  "title": "Atlas 项目简报",
  "subtitle": "2026 年 9 月发布评审",
  "evidence_policy": "optional",
  "sections": [
    {
      "id": "overview",
      "heading": "结论：按期发布，风险集中在支付回调",
      "level": 1,
      "blocks": [
        {"id": "p-owner", "type": "paragraph",
         "text": "负责人林琪，计划发布日期 2026-09-15。"},
        {"type": "callout", "style": "warning",
         "text": "支付回调偶发超时是唯一未收敛的风险。"}
      ]
    },
    {
      "id": "plan",
      "heading": "发布计划",
      "level": 2,
      "blocks": [
        {"type": "table",
         "headers": ["阶段", "时间", "门槛"],
         "rows": [["灰度", "09-08", "错误率低于 1%"], ["全量", "09-15", null]]}
      ]
    }
  ],
  "claims": [
    {"claim_id": "c-date", "text": "发布日期 2026-09-15",
     "evidence_ids": ["notes/project.md"],
     "target_type": "docx_paragraph", "target_id": "p-owner"}
  ]
}
```

## 不要做的事

- 不要传 `font`、`page_size`、`margin`、`line_spacing` 之类字段——Renderer 固定这些，schema 会拒。
- 不要为了"控制排版"把一节拆成几十个只有一句话的 paragraph。
- 不要在 `text` 里写 Markdown 语法（`##`、`**`、`- `）：它会被原样渲染成正文字符。
- 不要在 paragraph 上填 items/rows，或在 bullets/table 上填备用 text；这些字段可能被接受但不会显示。
- 不要复用 section id 或 paragraph id。
