这是 WorkPilot 手动测试的唯一授权目录。

测试时请从当前目录选择一个或多个具体源文件。发送任务时，WorkPilot 会把当前
目录作为会话读写授权根。不要选择或授权它的父目录 manual-test-kit。

源文件：
- 01_atlas_project_facts.txt：事实读取和 Word 生成。
- 02_weekly_report_to_edit.txt：diff 与审批。
- 03_budget_source.txt：Excel 生成。
- 04_slide_source.txt：PowerPoint 生成。
- 05_document_requirements.txt：Word 版式要求。
- 06_long_task_source.txt：中断与恢复。
- Atlas-Reading-Sample.pdf：PDF 阅读与引用。

输出目录 output 会由 WorkPilot 在测试过程中创建。
