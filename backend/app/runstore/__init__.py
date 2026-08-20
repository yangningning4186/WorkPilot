"""run / 事件 / checkpoint 的存储适配层（见 ADR-0011）。

这一层既不是框架也不是产品：

- 它知道 `agent_runs` / `run_events` / `agent_checkpoints` 的表结构，
  也知道桌面 Cowork 的 SQLite 旁路（ADR-0010），所以**不是**框架层——
  `agent_core` 里不该出现建表语句。
- 但 RAG 与 Cowork 两条业务线都用它，所以**不属于任何一个产品**。

对齐 pi 的划法：`pi-agent-core` 管"事件是什么形状"（本项目在
`app/agent_core/contracts.py`：`RunEvent` / `RunRecord` / `TERMINAL_RUN_STATUSES`），
"会话持久化"归业务侧。本包就是那个持久化实现，从 `app/services/` 与
`app/agent/` 里抽出来，好让 Step 3 拆产品包时它不用跟着二选一。

依赖方向：`{rag, cowork} → runstore → agent_core → workpilot_ai`。
本包不许 import `app.rag` 与 `app.cowork`。
"""
