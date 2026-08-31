# WorkbookSpec 字段速查

`spec` 是下面这个对象，`extra="forbid"`：**未列出的字段一律被拒**。

## 顶层

| 字段 | 必填 | 说明 |
|---|---|---|
| `schema_version` | 是 | 固定 `1` |
| `artifact_type` | 是 | 固定 `"xlsx"` |
| `title` | 是 | ≤300，只写入文件元数据；需要可见标题时另放 title style 的 cell |
| `purpose` / `audience` | 否 | 意图说明，不进单元格 |
| `sheets` | 是 | 1–100 张 |
| `claims` | — | **必须为空**。XLSX 不支持 claim 绑定，写了直接被拒 |

## sheets[]

| 字段 | 必填 | 约束 |
|---|---|---|
| `name` | 是 | ≤31 字符，大小写不敏感地唯一；不能含 `\\ / * ? [ ] :` |
| `freeze_panes` | 否 | 单元格地址如 `"A2"`（冻结首行）、`"B2"`（冻结首行+首列）。不写就没有冻结 |
| `cells` | 否 | ≤20000 个散点单元格 |
| `tables` | 否 | ≤100 个矩形表 |
| `charts` | 否 | ≤50 个图表 |

## cells[]

```json
{"address": "B2", "value": 120, "style": "currency"}
{"address": "B5", "formula": "=SUM(B2:B4)", "style": "currency"}
{"address": "B8", "style": "percent"}
```

- `address` 形如 `A1`、`AB12`。字段正则不代表 Excel 网格边界校验；实际单元格、表格展开范围与
  图表引用都必须落在 `A1:XFD1048576` 内。
- `value` 与 `formula` **至多填一个**；`formula` 必须以 `=` 开头。两者都省略时是 style-only cell，
  只应用格式，适合在 table 写值前给已知数据地址设置 number format。
- `style`：`normal`（默认）/ `title` / `header` / `metric` / `currency` / `percent` / `date`。
  `currency` 固定为人民币，`percent` 期待小数比例；`date` 只设置显示格式。
- `value` 可以是 str / int / float / bool / null。数字别写成 `"120"`。当前 JSON Scalar 不含
  date/datetime，所以 ISO 日期字符串仍是文本，即使 style=date；不要声称它具备原生日期计算/排序语义。
- 需要真正的 Excel 日期值时可写 `formula: "=DATE(2026,8,30)"` 并配 `style: date`；这属于待重算
  公式，交付时遵守公式验证限制。

## tables[]

```json
{"name": "Orders", "anchor": "A1",
 "headers": ["区域", "金额"], "rows": [["华东", 420], ["华北", 80]]}
```

`name` 必须是 `[A-Za-z_][A-Za-z0-9_.]*`（不能用中文、不能以数字开头）。
名称在**整个 workbook** 内必须唯一。`headers` 每项必须非空，去掉首尾空白后应大小写
不敏感地唯一；否则 Excel 可能自动改名或提示修复。每行长度必须等于 `headers` 长度，缺的补 `null`。
从 `anchor` 向右、向下展开后不得越过 Excel 网格边界。

## charts[]

```json
{"chart_type": "column", "title": "各区域金额",
 "data_range": "Summary!B1:B3", "categories_range": "Summary!A2:A3", "anchor": "D2"}
```

`chart_type` 只有 `bar` / `column` / `line`。区域必须指向本工作簿内已经写了值的范围。
Renderer 使用 `titles_from_data=True`：`data_range` 必须包含系列标题行，`categories_range` 只包含
数据行，二者的数据点数量一致。Sheet 名含空格时写成 `'Sales Detail'!B1:B8`。

## 最小可用示例

```json
{
  "schema_version": 1,
  "artifact_type": "xlsx",
  "title": "订单区域汇总",
  "sheets": [
    {
      "name": "Summary",
      "freeze_panes": "A2",
      "cells": [
        {"address": "A1", "value": "区域", "style": "header"},
        {"address": "B1", "value": "金额", "style": "header"},
        {"address": "A2", "value": "华东"}, {"address": "B2", "value": 420, "style": "currency"},
        {"address": "A3", "value": "华北"}, {"address": "B3", "value": 80, "style": "currency"},
        {"address": "A4", "value": "合计", "style": "metric"},
        {"address": "B4", "formula": "=SUM(B2:B3)", "style": "currency"}
      ],
      "charts": [
        {"chart_type": "column", "title": "各区域金额",
         "data_range": "Summary!B1:B3", "categories_range": "Summary!A2:A3", "anchor": "D2"}
      ]
    },
    {
      "name": "Raw",
      "freeze_panes": "A2",
      "tables": [
        {"name": "Orders", "anchor": "A1",
         "headers": ["订单号", "区域", "金额"],
         "rows": [["O-1", "华东", 420], ["O-2", "华北", 80]]}
      ]
    }
  ]
}
```

## 不要做的事

- 不要传 `column_width`、`font`、`fill`、`number_format`——Renderer 固定这些。
- 不要用 `cells` 一格一格地铺一张大表：那用 `tables`，行数上限是 100000。
- 不要把合计写成常量。评分看的就是"改一个明细，Summary 会不会跟着变"。
- 不要使用含 `[`/`]` 的外部工作簿或 structured-reference 公式；当前安全检查会把它们视为外部引用。
- 不要让 cell/table/chart 区域无意重叠，也不要复用 table name。
