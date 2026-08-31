# PresentationSpec 字段速查

`spec` 是下面这个对象，`extra="forbid"`：**未列出的字段一律被拒**。标准 layout 不传坐标、
尺寸或字号；只有 `canvas.elements[]` 使用安全区百分比边界盒与白名单字号。

## 顶层

| 字段 | 必填 | 说明 |
|---|---|---|
| `schema_version` | 是 | 固定 `1` |
| `artifact_type` | 是 | 固定 `"pptx"` |
| `title` | 是 | ≤300 |
| `purpose` / `audience` | 否 | `audience` 不进页面；`purpose` 会写元数据，且封面未给 subtitle/body 时会作为副标题兜底 |
| `evidence_policy` | 否 | `none` / `optional`（默认）/ `required` |
| `claims` | 否 | `target_type` 必须是 `pptx_slide`，`target_id` 必须是存在的 slide id |
| `visual_kit` | 否 | 默认 `workpilot-clean`；必须是 `assets/templates/catalog.json` 中的 id |
| `theme` | 否 | 见下 |
| `slides` | 是 | 1–100 页，id 必须唯一 |

## visual_kit 与 theme（改之前先想清楚有没有必要）

优先按 `visual-kits.md` 选择一个套件。Renderer 先应用套件主题，再应用用户显式给出的 theme 字段；
因此通常只填 `visual_kit`，不要复制一整套 theme。用户给了品牌色/字体硬约束时才覆盖对应字段。

`name` 是主题标识；`background` / `surface` / `text_primary` / `text_secondary` / `accent` / `positive` /
`warning` 都是 6 位十六进制（不带 `#`）；`title_font` / `body_font` / `east_asia_font`
是字体名。默认视觉套件是 `workpilot-clean` + Arial + Microsoft YaHei。
**换字体前确认本机装了它**，否则渲染会静默回退。

## slides[]：layout 决定哪些字段生效

| layout | 用得上的字段 | 承载量 |
|---|---|---|
| `title` | `title`, `subtitle` 或 `body`（二选一）；可选图片字段 | 封面，一句话 + 右侧主视觉 |
| `statement` | `title`, `body`, `subtitle` | 一句话结论（`body` 不填就渲染 `title`） |
| `section` | `title`, `body`, `subtitle`；可选图片字段 | 章节转场 + 可选右侧主视觉 |
| `two_column` / `comparison` | `left_title`+`left_items`, `right_title`+`right_items` | 每栏 ≤5 条 |
| `big_number` | `metrics[]`（`value` ≤30, `label` ≤80, `detail` ≤120），可选 `body` 结论条 | ≤4 个指标 |
| `chart` | `chart`（见下），可选 `body` 结论条 | 1 张图 |
| `image_text` | `image_path`（Skill 级必填）, `image_alt`, `image_caption`, `image_fit`, `bullets`/`body` | 1 图 + 少量文字 |
| `quote` | `body`, `quote_attribution` | 一段引文 |
| `timeline` | `timeline[]`（`label` ≤40, `title` ≤80, `detail` ≤160） | ≤4 个节点 |
| `matrix` | `matrix[]`（`x`/`y` ≤80, `label` ≤100） | 2–4 个对象；显示 label 与 x/y 维度 |
| `cards` | `cards[]`（2–4 个；每个 `title`、`detail`，可选 `kicker`） | 原生并列卡片；不是二维矩阵 |
| `activity` | `activity_prompt`、`activity_steps`（2–4）、`activity_timebox`、`activity_debrief` | 可直接主持的互动/练习页 |
| `diagram` | `diagram.kind/nodes`，hierarchy 另需 `edges` | 流程、循环、层级、漏斗、金字塔；2–8 个可编辑节点 |
| `canvas` | `canvas.elements` | 固定组件无法表达的特殊空间关系；0–100 安全区坐标 |

每页通用字段：`id`（必填，唯一）、`title`（必填 ≤300）、`role`（`hero`/`supporting`/`transition`）、
`rhythm`（`peak`/`valley`）、`notes`（≤4000，写来源）。

页面级 **`image_path` 只能出现在 `title`、`section` 或 `image_text` 页**；`image_caption`/`image_alt`/
`image_fit` 不能脱离它单独出现。`canvas` 的图片使用 `canvas.elements[].image_path/image_alt/image_fit`，
仍必须是已授权本地文件。

Schema 会按 layout 拒绝缺失的必需内容和不会显示的内容字段：左右栏必须完整，big_number/chart/
image_text/quote/timeline/matrix/cards/activity/diagram/canvas 必须提供对应对象；`title` 的 subtitle/body 与 `image_text` 的
bullets/body 不能同时提供；隐藏在错误字段里的备用文案不会被接受。只按
`references/new-deck.md` 填当前 Renderer 消费的字段。

`diagram` 与 `canvas` 的完整节点数、拓扑、坐标、重叠、颜色和文字容量契约只在需要高级构图时读取
`references/diagrams-and-canvas.md`；不要凭记忆猜字段。

v1 没有独立的纯 bullets layout。`bullets` 只在 `image_text` 消费，不再作为其它 layout 的空内容
兜底。普通清单应拆成 `cards` 或 `two_column` 的有意义分组，
或拆成多页 `statement`/其他匹配内容结构的页面。

## chart

```json
{"chart_type": "column", "categories": ["华东", "华北"],
 "series": [{"name": "金额", "values": [420, 80]}], "unit": "元"}
```

`chart_type` 只有 `bar` / `column` / `line`；`categories` ≤12；`series` ≤4，且类别数 × 系列数至少为 2，
每个 `values` 长度必须等于 `categories` 长度。`unit` 当前不会自动显示在图表上；单位影响理解时，
把它写进页标题或系列名，并在 notes 保留完整口径。

## 最小可用示例

```json
{
  "schema_version": 1,
  "artifact_type": "pptx",
  "title": "Atlas 项目进展",
  "visual_kit": "consulting-02",
  "slides": [
    {"id": "cover", "role": "hero", "rhythm": "peak", "layout": "title",
     "title": "Atlas 按期发布", "subtitle": "2026-09 发布评审"},
    {"id": "thesis", "layout": "statement",
     "title": "结论", "body": "灰度指标达标，唯一未收敛的是支付回调超时。"},
    {"id": "plan", "layout": "timeline", "title": "发布计划",
     "timeline": [
       {"label": "09-08", "title": "灰度", "detail": "错误率门槛 1%"},
       {"label": "09-15", "title": "全量", "detail": "需风险关闭"}
     ],
     "notes": "来源：meetings/weekly.md"},
    {"id": "risk", "layout": "two_column", "title": "风险与行动项",
     "left_title": "风险", "left_items": ["支付回调偶发超时"],
     "right_title": "行动项", "right_items": ["补重试与告警", "回归压测"]}
  ],
  "claims": [
    {"claim_id": "c-date", "text": "全量发布日期 2026-09-15",
     "evidence_ids": ["notes/project.md"],
     "target_type": "pptx_slide", "target_id": "plan"}
  ]
}
```

## 不要做的事

- 标准 layout 不要传 `font_size`、`left`、`top`、`width`；canvas 只接受元素内的
  `x/y/width/height` 百分比与白名单 `font_size`，没有 `left/top/zIndex`。
- 不要为了少拆一页把一条 bullet 写到 180 字符上限：那必然触发 `text_overload`。
- 不要让任意两张相邻页使用同一个 layout；每一处相邻重复都会计入 `layout_repetition` warning。
- 不要依赖 layout 的空内容兜底；按 new-deck 的必需内容矩阵做预检。
