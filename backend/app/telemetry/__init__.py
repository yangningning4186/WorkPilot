"""LLM 调用、GPU 批次与费用预留的 PostgreSQL 适配器。

契约、schema 与成本口径在 `packages/workpilot-telemetry/`（一个依赖表为空的包）；
这里只有"写进哪张表"。两者的分界线：

- `workpilot_telemetry.cost.BatchCost` 定义摊销公式 → `cost_report.py` 从 SQL 装载它
- `workpilot_telemetry.budget.BudgetGuard` 定义闸门语义 → `model_budget.py` 实现它
- `workpilot_telemetry.records.AuditRecord` 定义调用记录 → `llm_calls.py` 落库

`tests/test_telemetry_conformance.py` 用包内的 `conformance` 套件验证本层实现
确实满足那些语义——Protocol 的签名管不住"预留没结算就不该扣钱"。
"""
