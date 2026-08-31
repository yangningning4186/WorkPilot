# Existing document

先做只读盘点：段落与 run 样式、表格、页眉页脚、节、编号、图片关系、修订、批注、内容控件和
嵌入对象。先定位真实对象再改，不凭示例猜段落或表格索引；`paragraph.text = ...` 会抹掉 run 级
混合格式，保留格式时逐 run 或精确改 OOXML。

局部保真编辑使用 `run_sandbox`，设置 `artifact_operation="edit_existing"`，并把源文件列在
`source_paths`。脚本从 `$WORKPILOT_INPUTS` 读取，在 `$WORKPILOT_WORK` 处理，把**不同文件名**的
候选写到 `$WORKPILOT_OUTPUTS`，命令直接调用 `$WORKPILOT_PYTHON`。Sandbox v2 不允许覆盖已存在
目标，所以这条路径只能另存，不能承诺原地替换。默认名为 `<stem>-workpilot.docx`；若已存在则加
时间戳，不能让提交阶段才因撞名失败。

只改用户点名对象。保存后重新打开并核对未涉及的节、样式、表格、页眉页脚、关系、修订/批注数量；
再做 ZIP 完整性和最终候选版面检查。候选通过前不触碰原件。若复杂对象在 python-docx 往返后丢失，
停止提交并准确说明限制，不用新建文档冒充保真编辑。
