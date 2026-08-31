# Public artifact benchmarks

`catalog.json` 是外部 benchmark 的受控接入清单，不包含第三方数据副本，也不会自动联网下载。
每个来源先记录任务适配价值、split 策略、许可证状态和已知限制；只有许可证、固定 revision、
数据哈希、adapter 和人工复核都完成后，`integration_status` 才能改为 `adapter_ready`。

这避免三个常见错误：把上游 test 当开发集、把代码许可证误当材料许可证，以及把模型 Judge
分数混进确定性结构/安全门禁。

离线检查：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.artifact_suite public-catalog
```

当前真正可运行的本地集合是 `eval/suites/artifact-rendering-dev-v1.json`；公开集合仍按各自状态
逐步接入，不能在报告中写成“已经跑过”。
