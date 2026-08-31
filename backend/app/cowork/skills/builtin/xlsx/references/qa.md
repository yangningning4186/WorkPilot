# XLSX QA

以最终保存的 XLSX 为验收对象，不以 Spec 或内存 workbook 为准。

1. **结构**：重新打开；核对 sheet 顺序/名称、非空 sheet、冻结窗格、表格名称全局唯一，表头非空且唯一，
   坐标未越过 `XFD1048576`，且 cell/table/chart 区域没有意外重叠。
2. **数据类型**：数字和布尔不是字符串；百分比底层是小数；ISO 日期文本不得报告为原生日期。
3. **公式**：逐个读回公式文本，检查范围、跨 sheet 引用、除零/空值保护、业务硬编码、公式模式
   中断、范围少一行、被常量覆盖的公式，以及是否含 `#REF!`、`#DIV/0!`、`#VALUE!`、`#NAME?`
   或外部/DDE 形式。完整审计按 `references/audit.md`。
4. **图表**：检查 series formula、categories formula、数据点数量、系列名与 anchor；data range
   必须含标题行，categories range 不含标题行。
5. **可用性**：表头完整、长文本换行、关键数字未截断、Summary 引用明细而非重复硬编码。
6. **兼容与安全**：无外部 OOXML 关系、宏、Power Query 或外部连接；这些对象不是“打开正常”
   就代表保真。
7. **视觉与重算**：有 LibreOffice 时检查真实预览和公式重算；不可用时只报告结构/静态公式检查，
   并说明公式值将在 Excel/WPS 打开时重算。

修复后重新验证最终文件的全部 sheet，不只看改过的区域。
