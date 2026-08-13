# E0 · Embedding 模型选型

| | |
|---|---|
| **日期** | 2026-08-14 |
| **git_sha** | 跑批时自动记录 |
| **数据集** | embedding-smoke（30 blocks / 20 queries） |
| **候选** | Ollama `bge-m3:latest` / `qwen3-embedding:0.6b` |
| **fallback** | 禁用；两个候选分别直连同一本机 Ollama |
| **配置** | [`config/embedding-bakeoff.json`](../../config/embedding-bakeoff.json) |

## 1. 假设（跑批前）

Qwen3-Embedding-0.6B 使用检索任务 instruction 后，在中英混合与意图改写查询上的
span recall 可能优于 bge-m3，但单条查询延迟会更高。若两者质量差异小于 1-2 pt，
优先采用更快、部署更成熟的 bge-m3；若 Qwen 的质量优势明确，则接受额外延迟。

## 2. 控制变量

- 相同的 30 个 block、20 个 query、gold relevant block IDs。
- 相同的 cosine 排序、`top_k=10` 和 800 estimated-token context budget。
- 相同机器、同一 Ollama 版本、串行单请求延迟口径。
- 唯一模型侧差异是 embedding 模型；Qwen query 使用其推荐的 instruction，document 不加。
- 每个模型单独生成 corpus vectors，实验向量不写入正式 `chunks`。

## 3. 结果

待跑批后填写。

## 4. 决策边界

这是一组公开 smoke fixture，只能发现明显退化并验证流程，不能据此宣称最终选型。
正式决策前需要用至少 40 条真实私人语料 query 和人工 gold spans 复跑；阈值也必须按最终模型
重新校准，不能沿用另一模型的 cosine score。
