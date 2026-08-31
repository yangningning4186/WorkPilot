---
name: xlsx
description: 创建、分析、检查或安全修改本地 Excel/XLSX；新建工作簿时先规划工作表与坐标，再构造 WorkbookSpec 交给固定 Renderer 生成并验证
metadata:
  kind: artifact
  trigger:
    - 用户要求创建、修改、分析或交付 Excel/XLSX 文件
    - 需要公式、表格、冻结窗格、样式或图表
  anti_trigger:
    - 用户只需要 CSV/TSV 文本
    - 用户处理的是飞书电子表格或多维表格
  tools:
    - list_files
    - read_file
    - load_skill
    - load_skill_resource
    - render_artifact
    - run_sandbox
    - run_shell
  runtime:
    profile: artifact-python
  compatibility:
    - openpyxl>=3.1
    - LibreOffice optional
  status: active
---

# 你负责的和你负责不了的

Renderer 已经固定：字体与配色、表头填充、隐藏网格线、按中英文显示宽度算列宽、
长文本换行后自动加行高、`currency`/`percent`/`date` 三种 number format、图表尺寸。
**这些不接受 spec 覆盖。**

你决定的是数据本身和它的组织方式：

1. 拆几张 sheet、哪张是 Summary、哪张是可追溯的明细
2. 每个值是 `value` 还是 `formula`——推导值必须是公式，硬编码结果算缺陷
3. 每个 cell 的 `style`（它决定数字怎么显示，是你唯一的格式开关）
4. `freeze_panes` 设在哪（**不写就没有冻结**）、图表引用哪段区域

# 决定路径

- **只读**：`run_shell` 里 `python -c` 在内存中查 sheet、类型和公式，直接调用 `$WORKPILOT_PYTHON`；
  不得创建辅助脚本、备份或产物。
- **保真编辑已有、无宏工作簿**：先读 `references/existing-workbook.md`，用 `run_sandbox`。
  含宏或外部连接的工作簿不由本 Skill 改写。
- **新建**：先读 `references/patterns.md` 判断场景原型（顺带确认用户要的功能这套 Renderer
  做不做得到），再读 `references/spec.md` 拿字段与最小可用示例，构造 `WorkbookSpec`。若本轮尚无
  Office Brief/source map，先 `load_skill("office-workflow")`；已由它交接时不要重复加载。
- **审计/检查公式**：只读检查先读 `references/audit.md`，按范围报告问题；用户明确说“检查并修复”
  才进入已有工作簿候选编辑。

# 工作流

1. 消费 Office Brief 与 source map；直接进入本 Skill 时才从请求与附件推断输入数据、模板/样例
   意图、单位、类型口径、推导指标与图表。只问会改变表结构的缺口；空模板不得擅自填样例数据。
2. 读 `references/patterns.md` 判断场景原型；用户要合并单元格、条件格式或下拉列表时，
   **先按那里的限制说明做不到什么**，再往下走。
3. 读 `references/spec.md`，先在纸面确定每张 sheet 的标题区、表头、数据、公式、汇总和图表锚点，
   检查区域不重叠后再写 Spec；不建空白 sheet。
4. 数字、布尔保持原生类型；比例用小数值配 `percent`。`WorkbookSpec v1` 的 JSON 值暂不支持
   真正的 date/datetime；需要日期语义时用受控 `DATE(...)` 公式或改走保真编辑，不能把 ISO 文本
   说成原生日期。
5. 调用 `render_artifact`；覆盖已有文件必须带刚读出来的 `baseline_sha256`。
6. 提交前读 `references/qa.md`；除显式错误外检查硬编码假设、公式模式中断、范围少一行、被常量
   覆盖的公式、跨 sheet 对齐和单位尺度；按 Validator 的公式、结构与安全结果交付。

# 被拒绝时怎么改

| 拒绝理由 | 该怎么改 |
|---|---|
| `formula 必须以 = 开头` | 补 `=`；或者这本来就是常量，改用 `value` |
| `cell 不能同时设置 value 和 formula` | 二选一。推导值留 `formula`，删掉 `value` |
| `table 每行列数必须与 headers 一致` | 补 `null` 占位，不要删表头 |
| `sheet name 必须唯一（不区分大小写）` | 改名；`Summary` 与 `summary` 算同一个 |
| openpyxl 报重复 table name | `tables[].name` 在整个工作簿内必须唯一，不只是当前 sheet |
| Excel 打开时修复表格/坐标越界 | 表头改为非空且唯一；确保 cell、table 展开区域与 chart range 都在 `A1:XFD1048576` 内 |
| `ArtifactManifest v1 尚不支持 XLSX claim target` | **XLSX 不能带 `claims`**，删掉；来源写进 Summary 的说明单元格 |
| Validator `potential_clipping` | 单元格文本太长，拆列或缩短，不要靠调列宽（列宽是 Renderer 算的） |
| Validator `dde_or_external_formula` | 公式引用了外部工作簿或 DDE，改为只引用本工作簿内区域 |
| Validator `formulas` 显式错误 | 出现了 `#REF!`/`#DIV/0!`，检查区域引用与除零分支 |
| 图表只有一个数据点或系列名错误 | `data_range` 要包含表头行，`categories_range` 只包含数据行；两者数据长度一致 |

# 内容判断（Renderer 不管，但这是评分项）

- 每张 sheet 有明确用途；Summary 只放决策要看的几个数，明细表保留完整原始记录。
- 汇总、占比、同比一律写成公式引用明细区域，让用户改一个数就能重算。
- 人民币金额用 `currency`、比例用 `percent`；`percent` 的值写 `0.85` 才显示为 85%。其他币种在
  表头写单位并使用普通数值，不能套用固定的人民币格式。
- 图表只表达一个结论；可见类别超过约 12 项或系列超过 4 个时通常应聚合或拆图，避免 Summary
  变成难读的明细图（这是可读性规则，不是 WorkbookSpec 字段上限）。
- 公式只引用工作簿内安全区域；禁用 DDE 与外部工作簿引用。
- 表格表头必须非空且唯一；在写 Spec 前算完表格右下角，不得越过 Excel 网格边界。
- 用户要空模板时只保留表头、说明和带空值保护的公式；只有用户要示例或用途不明且更像演示表时，
  才放少量明显虚构、无敏感信息且与公式自洽的样例。

# 交付判定与重试上限

Validator 给的是**加权综合分**：structural 30% / semantic 25% / visual 25% / evidence 20%，
维度内每个 failed 扣 50、warning 扣 10，没跑的维度记满分（"没测量"不等于"有问题"）。
**安全维度不参与加权，它是一票否决**：任一安全检查 failed，综合分直接归零、不可交付。
不存在"92 分的安全问题"——不要把它当成一条可以解释过去的 warning。

被拒绝或未通过时，**同一份产物最多改两次重试**。第三次仍不通过就停下，向用户说明：
Renderer/Validator 的原话、你已经改过什么、以及建议的取舍（删内容 / 拆文件 / 换格式），
由用户决定下一步。不要继续试，也不要转成脚本绕过这道门。

# 对用户怎么说

对外只用「工作簿 / 工作表 / 表头 / 公式 / 冻结首行」这类词。`WorkbookSpec、render_artifact、baseline_sha256、cells、anchor` 是内部实现，
不要出现在给用户的话里，也不要写进待办标题。报告验证结果时说"结构检查通过、版面预览在
这台机器上不可用"，而不是"visual.status=not_run"。

# 验证

按 `references/qa.md` 检查 sheet 名、空白 sheet、cell type、公式数与显式错误、合并区域、图表关系、
外部引用、DDE、疑似列宽截断。**公式结果的重算依赖本机 LibreOffice：没有它时只能报告
“公式已写入并做静态检查”，不得声称“结果已计算并核对”。**
