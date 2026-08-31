# PdfSpec 字段速查

只用于**新建** PDF。`extra="forbid"`：未列出的字段一律被拒。

## 顶层

| 字段 | 必填 | 说明 |
|---|---|---|
| `schema_version` | 是 | 固定 `1` |
| `artifact_type` | 是 | 固定 `"pdf"` |
| `title` | 是 | ≤300，渲染成首页大标题 |
| `summary` | 否 | ≤4000，标题下的导读段 |
| `purpose` / `audience` | 否 | `purpose` 会显示在标题下并写入 metadata subject；`audience` 只作意图说明，不进正文 |
| `evidence_policy` | 否 | `none` / `optional`（默认）/ `required` |
| `claims` | 否 | `target_type` 必须是 `pdf_section`，`target_id` 是 section `id` |
| `sections` | 是 | 1–100 节 |

## sections[]

| 字段 | 必填 | 约束 |
|---|---|---|
| `id` | 是 | `[A-Za-z0-9._:-]+`，≤120，claim 靶子就是它 |
| `heading` | 是 | ≤300 |
| `blocks` | 否 | ≤500，block 结构与 DOCX 完全一致 |

**PDF 没有 `level`**：所有 section 都是同一层。需要层次感就靠 heading 措辞和顺序，
不要试图传 `level` 字段。

所有 section `id` 应唯一。Schema v1 目前不完整拒绝重复 id，但 claim 会因此产生歧义，按 Skill
质量契约视为错误。

## blocks[]（与 DocumentSpec 同构）

| type | 用哪些字段 |
|---|---|
| `paragraph` | `text`、`style`；style 会保留语义，但当前 PDF Renderer 不提供明显外观差异 |
| `bullets` | `items`（≤100 条） |
| `table` | `headers`（≤10）+ `rows`（每行列数必须一致） |
| `quote` | `text` |
| `callout` | `text`；style 可记录语义，当前 PDF 外观不区分 warning/positive |
| `image` | `image_path` + `image_alt`（必填）+ 可选 `image_caption` / `image_width_inches` |

`style`：`normal` / `lead` / `caption` / `positive` / `warning`。

Schema 只严格校验图片字段与表格形状，其他 block 的无效字段可能被接受但静默忽略。每个 block 只填
上表对应字段；数据表应提供非空 headers。

## 最小可用示例

```json
{
  "schema_version": 1,
  "artifact_type": "pdf",
  "title": "周会纪要",
  "summary": "2026-09 第一周，发布策略与验收门槛。",
  "sections": [
    {
      "id": "decision",
      "heading": "决定",
      "blocks": [
        {"type": "paragraph", "text": "先灰度发布，再全量上线。"},
        {"type": "callout", "style": "warning", "text": "全量前必须关闭支付回调风险。"}
      ]
    },
    {
      "id": "actions",
      "heading": "行动项",
      "blocks": [
        {"type": "table",
         "headers": ["负责人", "事项", "期限"],
         "rows": [["王宁", "补重试与告警", "09-10"]]}
      ]
    }
  ],
  "claims": [
    {"claim_id": "c-gate", "text": "验收门槛为错误率低于 1%",
     "evidence_ids": ["meetings/weekly.md"],
     "target_type": "pdf_section", "target_id": "decision"}
  ]
}
```

## 不要做的事

- 不要传 `page_size`、`margin`、`font`——A4 与边距是 Renderer 固定的。
- 不要把长表格塞进 PDF：列多了必然触发 `text_bounds`。宽数据交付 XLSX，PDF 里只放结论。
- 不要用空 section 制造分页；它会留下没有正文价值的标题，分页应交给流式 Renderer。
- 不要复用 section id，也不要在 bullets/table 上放备用 text 期待显示。
