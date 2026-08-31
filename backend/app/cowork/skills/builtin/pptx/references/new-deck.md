# New deck

先按 `materials-and-story.md` 在本 Skill 内把材料、STORY 与 DESIGN 收敛成 `PresentationSpec`。每页先
写一句 takeaway，再选最适合它的信息结构。
Schema 会拒绝 layout 不消费的字段、缺失的必需内容，以及会被备用逻辑遮住的互斥字段；仍要按
下面的内容契约自检，因为“字段合法”不等于“页面信息充分”。

| layout | 必须提供 | 适合 | 当前行为与陷阱 |
|---|---|---|---|
| `title` | `title`；需要时 `subtitle` 或 `body` 二选一；可选本地主视觉 | 封面 | subtitle/body 同时提供会被拒绝；主视觉必须先核对 |
| `statement` | `body` 或可独立成句的 `title`，可选 `subtitle` | 单一结论 | 有 body 时 title 显示为语境标签；不承载 bullets |
| `section` | `title`，可选 `body`、`subtitle` 与本地主视觉 | 章节转场 | 有 body 时 title 在侧栏作章节标签；不承载细节清单 |
| `two_column` | 非空的 `left_title/left_items` 与 `right_title/right_items` | 主次分栏 | 空栏与通用 bullets 兜底已被契约拒绝；一边只有短句时换 statement |
| `comparison` | 两边同一口径的 title/items | before/after、方案对比 | 与 two_column 共用渲染结构；左右口径必须可比 |
| `big_number` | 1–4 个 `metrics` | KPI / 关键数字 | metrics 为空会被拒；普通页优先 2–4 个，单指标只作真正峰值页 |
| `chart` | `chart` | 趋势、分类对比 | chart 为空会被拒；至少两个可比较数据点 |
| `image_text` | `image_path`、准确 `image_alt`，以及 bullets/body 二选一 | 图像证据、产品/场景 | 没有图片或解释会被拒；bullets/body 同时提供也会被拒绝 |
| `quote` | `title`、`body`，有来源时 `quote_attribution` | 短引文 | title 是引文语境，body 是原文；自己的结论不能伪装成引文 |
| `timeline` | 2–4 个 `timeline` items | 时间/阶段序列 | 少于 2 个会被拒；非时间关系不要硬套 |
| `matrix` | 2–4 个 `matrix` items | 2×2 分类 | 少于 2 个会被拒；Renderer 会显示 label 与 x/y 说明 |
| `cards` | 2–4 个 `cards`，每个都有 title/detail | 并列主题、特征、案例、议程 | 只有标签的空卡会被拒为低密度；有真实二维轴时改 matrix |
| `activity` | prompt、2–4 个 steps、timebox、debrief | 班会、培训、工作坊、共创讨论 | “大家讨论一下”不算指令；必须说明多久、怎么做、如何复盘 |
| `diagram` | kind 与短 nodes；hierarchy 另需合法树 edges | 流程、循环、层级、漏斗、金字塔 | ≤8 节点；一般关系网和交叉线不要硬塞 |
| `canvas` | 2–24 个安全区 elements | 标准组件无法表达但关系明确的特殊构图 | 越界、>15% 重叠、任意色/脚本会被拒；不要拿它复刻普通 layout |

标准页面只描述标题、正文、要点、左右栏、指标或结构数据，坐标、边距与字号由 Renderer 决定。
仅 `canvas` 在 Renderer 保留的标题与安全区内使用百分比边界盒；详见 `diagrams-and-canvas.md`。
页面有可核验事实时，把 claim 的 `target_id` 指向 slide id。

密度同时有下限和上限：普通页最多 6 个要点；左右栏各 5 项；指标、时间线与矩阵最多 4 个对象；
图表类别最多 12 个、系列最多 4 个。普通 supporting 页必须达到 `standard`：至少两个互补信息区，
或一个证据型主视觉加解释。标题或正文装不下时拆页；内容不足时合并弱页或补支撑，不能靠大卡片、
装饰图或“极简”填空。

封面和章节页允许用 `image_path` 承载 L1 主视觉；`image_text` 用于“图像证据 + 解释”。三者都只
接受已经核对的本地图片/SVG。图片不是必填，但以场景、节日、产品或空间为主题且有可靠素材时，
不要退化成纯文字封面。

逐页内容检查：标题是结论而非栏目名；数字带单位与时间口径；图表页写清观察结论；比较页两边
使用相同维度；图片页的图能直接支撑本页主张；普通页有 L1 视觉结构。相邻页不得重复同一 layout；
连续 hero/statement 会让节奏失效，即使 layout 名不同也应调整。生成前按
`materials-and-story.md` 的密度与视觉角色门槛复核。

写最终文件前先按 `production-loop.md` 试制封面和代表页并查看返回图片；每页在提交后再由内置
PptxGenJS Renderer 从最终 PPTX 重新栅格化。只有全部页面生成 PNG 且没有 overflow、
unsupported shape、越界或重叠时，才算完成视觉验证。
