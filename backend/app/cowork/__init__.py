"""Cowork 产品：本地办公任务执行 Agent（见 ADR-0011 第三层）。

对齐 pi 的 `pi-coding-agent`——「最懂业务」的那一层：工具、扩展、会话持久化编排。
本包**不许** import `app.rag`：要用知识检索走 `app/knowledge_contracts.py` 里的
`RagService` Protocol，由 composition root（worker / api）注入实现。
"""
