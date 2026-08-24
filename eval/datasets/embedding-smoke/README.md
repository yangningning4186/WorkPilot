# embedding-smoke

E0 的公开、可提交 smoke fixture。语料由 WorkPilot 设计文档人工压缩而成，不包含私人资料。

- `corpus.jsonl`：30 个 block 级证据段，`context_tokens` 是统一规则下的估算上下文成本。
- `queries.jsonl`：20 个查询，其中 15 个 answerable、5 个 unanswerable。
- `relevant_ids` 可包含多个 block，用于 span recall 与 multi-hop 覆盖检查。

这份数据只负责验证跑批、发现明显方向和测量本机性能，不能据此最终选型。正式决策必须把
问题扩到至少 40 条，并替换/补充来自真实个人语料且经人工核验的 gold spans。
