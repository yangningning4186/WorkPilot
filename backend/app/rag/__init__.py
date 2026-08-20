"""RAG 产品：可溯源问答、固定综述与资料库（见 ADR-0011 第三层）。

与 `app/cowork/` 平级且互不 import。两者共享的东西只有三处：
框架层 `app/agent_core/`、存储层 `app/runstore/`、跨边界契约 `app/knowledge_contracts.py`。
"""
