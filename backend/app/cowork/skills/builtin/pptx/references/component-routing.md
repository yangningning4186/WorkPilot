# 内容组件路由

先判断信息关系，再选 PptxGenJS Renderer 当前能稳定生成的原生 PowerPoint 对象。`components.cjs`
统一维护视觉组件；模型只填写 `PresentationSpec`，不能把其它 PPT 系统的 JSX/组件 API 或任意
PptxGenJS 调用塞进 Spec。下面是可复用的设计意图与实际落点。

| 设计意图 | WorkPilot 落点 | 适用条件 | 不能做的事 |
|---|---|---|---|
| 容器 / 信息卡 | `cards`、`two_column`、`comparison` | 2–4 个并列但各自完整的信息单元 | 大盒子里只放一个标签 |
| 互动 / 练习 | `activity` | 有问题、2–4 步、时限与复盘问题 | 只写“大家讨论一下” |
| 数据图 | `chart` | 同单位的趋势或分类对比 | 编造数据、把截图当图表 |
| 关键指标 | `big_number` | 1–4 个有口径、有含义的数字 | 数字后没有判断 |
| 图片 | `title` / `section` 的主视觉，或 `image_text` | 已授权本地 PNG/JPEG/SVG，且与主张直接相关 | 在线 URL、占位符、小装饰冒充 L1 |
| 简单流程 / 阶段 | `timeline` | 2–4 个真实阶段或时间点 | 把无顺序的并列项硬做流程 |
| 二维分类 | `matrix` | x/y 维度真实存在 | 用矩阵代替普通四卡片 |
| 流程 / 循环 / 层级 / 漏斗 / 金字塔 | `diagram` | 2–8 个短节点且关系属于内置 kind | 一般关系网、交叉连线、长段落节点 |
| 特殊空间关系 | 受约束 `canvas` | 固定组件无法表达，且可在安全区内无重叠排布 | 用 canvas 重做普通 cards/timeline；任意脚本、颜色、z-index |
| 超过内置复杂度的结构图 | 安全 SVG + `image_text` | >8 节点或交叉关系复杂，且 SVG 可准确呈现 | 外部资源、脚本、`foreignObject` |
| 引文 | `quote` | 有真实原文和来源 | 把自己的结论伪装成引用 |

当前 PptxGenJS Renderer 不支持 PPT 原生表格、无限容器嵌套、图标库、动画、公式、二维码、任意超链接或
全画布任意 API。`canvas` 只开放标题下方安全区百分比坐标、白名单对象/字号/主题色和 ID 连接线；
遇到其它需求时改用受支持结构、把复杂明细放到 DOCX/XLSX 附件，或准确说明限制。不得照抄其它
Renderer 的 JSX/组件语法，也不得绕过固定 Renderer 运行临时 JavaScript。需要高级构图时读
`diagrams-and-canvas.md`。

## 图片与 SVG 的边界

- 具象人物、办公空间、校园活动、产品实景优先真实素材或当前环境真实可用的图片生成能力。
- 流程、矩阵、抽象关系、趋势箭头、纹样和可可信表达的风格化物件可以用 SVG。
- SVG 必须先作为本地素材核对，再通过 `image_path` 嵌入；图片文字应尽量少，需精确控制的标题和
  正文由 PPT 原生文字承担。

## 组件按需读取

只读本页需要的参考：图表读 `charts.md`，图片/SVG 读 `images.md`，复杂图示/特殊构图读
`diagrams-and-canvas.md`，中文文字读 `typography.md`。
不要一次把全部组件规则塞进上下文，也不要仅凭记忆构造未确认的字段。
