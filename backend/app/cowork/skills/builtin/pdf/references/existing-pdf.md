# Existing PDF

先检查加密状态、页数、MediaBox/CropBox、旋转、表单、批注、链接、附件和目标页面。默认另存为
`<原名>-workpilot.pdf`。合并/拆分必须保留页面顺序、旋转与裁剪框；脱敏必须创建 redaction
annotation 并执行 `apply_redactions()`，覆盖矩形不算脱敏。候选输出提交前逐页栅格化并复核边界。

使用 `run_sandbox` 时设置 `artifact_operation="edit_existing"`，把每个源 PDF 列入 `source_paths`。
脚本从 `$WORKPILOT_INPUTS` 读取，在 `$WORKPILOT_WORK` 处理，把**不同文件名**的候选写到
`$WORKPILOT_OUTPUTS`，命令直接调用 `$WORKPILOT_PYTHON`。Sandbox v2 不允许覆盖已存在目标，
因此这条路径不能承诺原地替换。若默认 `<原名>-workpilot.pdf` 已存在，则加时间戳。

提交前重新打开候选并核对：页数/顺序、每页尺寸/旋转/CropBox、链接/批注/表单/附件数量，以及逐页
栅格化。脱敏还必须在 `apply_redactions()` 后搜索原文、提取文本并检查对象流，确认内容确实不可
恢复；仅视觉上被遮住不算完成。表单扁平化会破坏继续编辑能力，只有用户明确要求才做。
