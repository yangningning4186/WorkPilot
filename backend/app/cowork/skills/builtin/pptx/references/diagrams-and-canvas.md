# 图示组件与受约束画布

仅在页面确实存在流程、循环、层级、漏斗、金字塔或无法由固定 layout 表达的空间关系时读取。
这套选型沿用 WorkBuddy `component-diagram` / `component-box` 的核心原则：先用稳定组件，节点较少时
使用本地可编辑对象，复杂拓扑才降级为经过核对的视觉素材；不把远程 Kroki 或任意脚本引入交付链。

## 选型顺序

1. `timeline`、`matrix`、`cards`、`comparison` 已能准确表达时，继续用固定 layout。
2. 需要明确关系语义时用 `diagram`；它负责自动排版和连线。
3. 关系合理但标准图示无法表达时才用 `canvas`；模型负责安全区内的边界盒。
4. 超过 8 个节点、交叉连接多、需要复杂曲线或高审美信息图时，不要强塞进 canvas；改用已核对的
   本地图片/SVG 与 `image_text`，或拆成多页。

## diagram

通用结构：

```json
{
  "layout": "diagram",
  "title": "从问题到落地形成闭环",
  "diagram": {
    "kind": "process",
    "nodes": [
      {"id": "observe", "title": "识别问题", "detail": "统一事实", "emphasis": "primary"},
      {"id": "pilot", "title": "小步试点", "detail": "验证假设"}
    ]
  }
}
```

`nodes[].id` 唯一；`title` 是短标签，`detail` 只补一层解释；`emphasis` 为 `primary` / `normal` /
`muted`。不要把正文段落塞进节点。

| kind | 节点 | 顺序与关系 |
|---|---:|---|
| `process` | 2–6 | `nodes` 即开始到结束；自动顺序连线；可选 `orientation: horizontal/vertical`，纵向最多 5 个 |
| `cycle` | 3–6 | `nodes` 按顺时针顺序自动闭环；可选 `center_label` |
| `hierarchy` | 3–8 | 必须提供 n-1 条 `edges`；只有一个根，每个子节点只有一个父节点，最多 4 层、每个父节点最多 4 个直接子节点 |
| `funnel` | 3–5 | `nodes` 从漏斗顶部到最窄结果依次排列，不提供 edges |
| `pyramid` | 3–5 | `nodes` 从顶层到基础层依次排列，不提供 edges |

`hierarchy.edges[]` 只接受 `source`、`target` 和可选短 `label`。其它 kind 禁止显式 edges，避免模型
构造 Renderer 无法稳定布线的一般图。连接线由 Renderer 先画、节点后画，保证箭头位于节点下层。

## canvas

Canvas 不是任意 PowerPoint API，而是 Renderer 标题下方安全内容区内的 **0–100 百分比坐标系**。
每个可见元素用 `x/y/width/height` 描述边界盒；Schema 保证 `x + width ≤ 100`、
`y + height ≤ 100`，并拒绝任意两个可见元素超过 15% 的大面积重叠、字号与高度不匹配、文字超过
边界容量，以及连接线穿过第三个元素。

支持的元素：

| type | 必需字段 | 可选字段与限制 |
|---|---|---|
| `text` | id、x/y/width/height、text | font_size 16–36、bold、align、valign、主题 `color_role` |
| `shape` | id、边界盒、title | detail；rectangle/rounded_rectangle/oval/chevron/hexagon；主题 fill_role；soft/solid |
| `image` | id、边界盒、已授权本地 image_path、准确 image_alt | image_fit 为 contain/cover；仍走统一路径授权与 SVG 安全检查 |
| `connector` | id、source_id、target_id | label、straight/elbow、主题 color_role；不能写裸坐标 |

```json
{
  "layout": "canvas",
  "title": "输入经过决策引擎转化为行动",
  "canvas": {
    "elements": [
      {"type": "shape", "id": "input", "x": 2, "y": 24, "width": 24, "height": 42,
       "title": "输入", "detail": "事实与需求"},
      {"type": "shape", "id": "engine", "x": 38, "y": 12, "width": 24, "height": 64,
       "shape": "hexagon", "title": "决策引擎", "detail": "规则 + 判断",
       "fill_role": "accent", "fill_style": "solid"},
      {"type": "shape", "id": "output", "x": 74, "y": 24, "width": 24, "height": 42,
       "title": "输出", "detail": "行动与结果"},
      {"type": "connector", "id": "e1", "source_id": "input", "target_id": "engine"},
      {"type": "connector", "id": "e2", "source_id": "engine", "target_id": "output"}
    ]
  }
}
```

Canvas 最多 24 个元素、10 条连接线，至少 2 个可见元素。颜色只能引用主题角色，不接受任意十六
进制色；字体、形状和字号都是白名单；不支持原始 SVG/HTML/CSS、旋转、动画、任意 z-index 或
外部 URL。需要文字覆盖在容器上时写进 `shape.title/detail`，不要叠放独立 text。

## 质检

`diagram` 与 `canvas` 都是证据型视觉结构，但必须服务页面 message。第一次使用某种图示时，把该页
加入 `preview_presentation.pages`；逐项检查节点顺序、箭头方向、层级归属、文字换行、连接线与标签，
然后才生成整稿。不能因为 Schema 通过就假设关系正确。
