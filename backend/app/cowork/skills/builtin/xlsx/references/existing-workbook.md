# Existing workbook

先检查 sheetnames、目标区域值与公式、命名区域、合并单元格、图表和外部链接。默认另存为新文件。
宏、Power Query、外部数据连接和复杂条件格式不在固定 Renderer 的保真承诺内；遇到这些结构应
停止重建并说明限制，不能交付一个打开正常但关键逻辑已经丢失的文件。

保真局部编辑使用 `run_sandbox`，设置 `artifact_operation="edit_existing"` 并把源文件放入
`source_paths`。脚本从 `$WORKPILOT_INPUTS` 读源文件，在 `$WORKPILOT_WORK` 处理，把**不同文件名**
的候选写到 `$WORKPILOT_OUTPUTS`，命令直接调用 `$WORKPILOT_PYTHON`。Sandbox v2 不允许覆盖已存在
目标，所以这条路径不能承诺原地修改。默认名为 `<stem>-workpilot.xlsx`；若已存在则加时间戳。

先读出真实 sheet 名、单元格类型、公式与坐标，再改用户点名范围。编辑后重新加载候选，核对未涉及
sheet、公式、样式、命名区域、合并区和图表数量没有丢失；需要继续编辑时以最新候选为准，不沿用
编辑前缓存的坐标或值。宏/外部连接一旦存在就停止，不通过另存为普通 XLSX 静默剥离。
