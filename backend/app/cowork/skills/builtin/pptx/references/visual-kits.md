# 模板与视觉套件

`assets/templates/catalog.json` 是套件真相源。模板来自用户指定的本地
`slides/slide_templates`，复制到 Skill 后以 SHA-256 锁定；
`scripts/visual_kits.py` 会在使用时校验对应二进制没有变化。

## 使用方式

顶层 `PresentationSpec.visual_kit` 选择一个套件，默认 `workpilot-clean`。套件提供：

- 从参考模板提炼的背景、表面、正文、次级文字、强调、正向和警告色；
- clean / editorial / luminous / organic / consulting / bold / tech 视觉家族；
- PptxGenJS 组件的标题规则、背景签名、容器和强调方式；
- 场景标签与源模板完整性锚点。

Renderer 会先应用套件 theme，再应用用户**明确给出的** theme 字段；未显式提供的默认 theme 值不会
盖掉套件。不要为了“更好看”随意手改多种颜色或字体；品牌硬约束才使用 override。

模板 PPTX 是设计参考和令牌来源，不是任意母版导入器。PptxGenJS 负责新建文件，不能读取现有
PPTX 并保留其母版/动画；不要声称生成文件逐字节继承了源模板。需要像素级保留用户模板时应说明
当前不支持；若用户接受重做，则把已有 deck 作为只读材料并用 PptxGenJS 另存，而不是静默改用
python-pptx 写入。

部分源模板自带图表数据工作簿或示例超链接，因此更不能把模板 ZIP、关系或嵌入对象复制进交付物。
Renderer 只消费 catalog 中已审阅的颜色、字体角色、视觉 family 与组件规则；最终 PPTX 必须重新
接受无外部关系、无活动内容检查。

## 场景路由

| 需要 | 优先候选 |
|---|---|
| 通用内部汇报、产品说明 | `workpilot-clean`, `project-kickoff-02` |
| 学术、课程、研究 | `academic-01`…`academic-04`，按正式/未来/自然主题选择 |
| 品牌、创意、消费体验 | `brand-01`…`brand-04` |
| 经营、战略、董事会、咨询 | `consulting-01`…`consulting-04` |
| 市场与用户研究 | `market-research-01` |
| 融资、发布、强提案 | `pitch-deck`, `brand-03` |
| 项目启动、团队共创 | `project-kickoff-01`, `project-kickoff-02` |

只把表当候选；最终按受众、场合、内容关系、用户明确视觉要求和素材风格选一个。不要在同一 deck
混用多个套件；章节差异通过版式和节奏表达，不换整套主题。

## 资源读取

- 想看全部套件 id、scene、family 和 theme：读 `assets/templates/catalog.json`。
- 想快速看模板外观：读 `assets/templates/Overview.png`。
- 需要核对源模板：按 catalog 的 `template_file` 读取对应 PPTX；不要一次把全部 16 份加载进上下文。
- 使用前由 `visual_kits.py` 校验 hash；hash 变化是硬错误，不自动接受新版本。

## 可复用 PptxGenJS 组件与辅助工具

`scripts/pptxgenjs/components.cjs` 维护统一的标题、表面、强调、图片框、连接线、页码、视觉家族背景
与布局审计。`helpers/image.js`、`helpers/layout.js`、`helpers/util.js` 从 slides Skill 的辅助库复制/适配，
分别负责等比裁切/包含、重叠与越界检查、安全外阴影。页面渲染必须复用这些能力，不在每个 layout
重新实现一套尺寸逻辑。

PptxGenJS 输出的文字、形状、图表和简单关系图保持可编辑。外部 SVG 先由 Python 安全层拒绝活动
内容并转成兼容 PNG；这一步只处理素材，不构造页面。最终只读验收器同时识别 PptxGenJS 的原生
`prstGeom=line` 连接线。

## 发布边界

源模板目录没有独立 LICENSE，仅有包内版权声明。可以在当前本地产品仓库内按用户要求复用；在把
这些二进制模板重新分发到公开产品、模板市场或第三方仓库前，必须另行确认授权。PptxGenJS 与
打包器依赖按各自锁定版本和许可证管理。
