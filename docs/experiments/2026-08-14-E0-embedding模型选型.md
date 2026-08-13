# E0 · Embedding 模型选型

| | |
|---|---|
| **日期** | 2026-08-14 |
| **git_sha** | `4b455762edc5a7beac5b25a3b60c80e3973b253c` |
| **数据集** | embedding-smoke（30 blocks / 20 queries） |
| **候选** | Ollama `bge-m3:latest` / `qwen3-embedding:0.6b` |
| **重复次数** | 3 次显式 warm-up 后跑批，指标取中位数 |
| **fallback** | 禁用；两个候选分别直连同一本机 Ollama |
| **配置** | [`config/embedding-bakeoff.json`](../../config/embedding-bakeoff.json) |
| **config_hash** | `25c63e0b2f8c05456f05780bf80c80dd11970cb12d2373ea16c4e08e405dbce5` |

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

正式报告：`20260813T172525Z`、`20260813T172547Z`、`20260813T172604Z`（UTC，原始 JSON
位于忽略提交的 `eval/outputs/embedding-bakeoff/`）。下表为三次中位数：

| 指标 | bge-m3 | Qwen3-Embedding-0.6B | 观察 |
|---|---:|---:|---|
| span recall@10 | 1.000 | 1.000 | 持平，fixture 已接近天花板 |
| nDCG@10 | **0.989** | 0.975 | bge-m3 +1.34 pt |
| MRR | **1.000** | 0.967 | Qwen 有 1 条题首个 gold 排第 2 |
| 800-token budget recall | 1.000 | 1.000 | 持平 |
| unanswerable AUROC | 1.000 | 1.000 | 数据太容易，不能外推 |
| 最优 smoke threshold | 0.5184 | 0.4902 | FAR/FRR 均为 0；不进入生产配置 |
| query p50 | 164.9 ms | **122.9 ms** | Qwen 快约 42 ms |
| query p95 | 168.5 ms | **126.5 ms** | Qwen 快约 42 ms |
| corpus throughput | **24.49 item/s** | 22.40 item/s | bge-m3 快约 9% |

三次性能波动很小：bge-m3 p95 为 165.7-171.6 ms，Qwen 为 125.4-127.4 ms。
显式 warm-up 修复前的首次数据把模型加载时间算入吞吐，已排除，不用于决策。

具体看样本时，Qwen 在 `q05`（“S1 最终会被解析成哪些原文定位信息？”）先召回了
语义相近的 `parsed-blocks`，gold `citation-anchor` 排第 2；bge-m3 把 gold 排第 1。
但只有 15 条 answerable query，无法做可信的 paired bootstrap，也不足以归纳稳定 badcase 模式。

## 4. 结论与决策

**有条件采纳 bge-m3 作为当前本地 embedding 基线。** 它在这组 smoke 上排序略优、批量灌库
略快；Qwen 的约 42 ms 查询优势相对后续生成耗时不是当前主要瓶颈。选择仍是临时的，不宣称
bge-m3 普遍优于 Qwen。

模型身份锁定为：

- bge-m3: `sha256-daec91ffb5dd0c27411bd71f29932917c49cf529a641d0168496c3a501e3062c`
- Qwen3-Embedding-0.6B: `sha256-06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439`

## 5. 决策边界与下一步

这是一组公开 smoke fixture，只能发现明显退化并验证流程，不能据此宣称最终选型。
正式决策前需要用至少 40 条真实私人语料 query 和人工 gold spans 复跑；阈值也必须按最终模型
重新校准，不能沿用另一模型的 cosine score。

- [ ] 从真实 Markdown/PDF 语料人工标注至少 40 条 query 和 gold spans。
- [ ] 增加更难的同主题干扰块与 15%-20% unanswerable 样本，打破当前天花板。
- [ ] 在真实集上复跑 bge-m3 / Qwen，并做逐样本 paired bootstrap。
- [ ] 只在真实集上确定 `REFUSAL_THRESHOLD`，当前 `0.35` 继续作为工程初值。
