# E3-FS：文件系统 KB 本地 cross-encoder 精排

**日期**：2026-08-23

**状态**：工程候选通过，human baseline / holdout 仍 pending

**结论**：采纳“RRF Top-10 → `bge-reranker-v2-m3` → Top-5”为可选本机质量层；开发环境显式开启，发布默认关闭并 fail-open。

## 1. 问题与假设

E2-FS 确认 hybrid 能保住 Top-10 召回，但产品实际只向生成层交付 Top-5，证据在前五的
排序仍不理想。本次假设是：**保留 dense + BM25 + RRF 做宽召回，再用本地 cross-encoder
对小候选集做相关性精排，能在不增加上下文数量的前提下提升 Top-5 nDCG 和 MRR。**

这不是预注册的独立确证实验：参数是在同一份 dev 集上选出，然后才跑最终严格配对。
因此数字只支持“值得作为工程候选”，不支持“真实用户整体提升 19 个点”。

## 2. 数据与不变量

- suite：`eval/suites/kb-rag-research-dev-v1.json`，53 篇论文，22 条可答 + 4 条不可答；
- 来源：`synthetic / pending_human_review`；
- KB：`rag-research / v1`，1,097 nodes，两臂使用同一 index fingerprint；
- embedding：`bge-m3:latest`，1024 维，同一 revision；
- retrieval：hybrid，chunk `512/64`，RRF `k=60`，dense/BM25 候选倍数 `2/2`；
- 正式指标深度：Top-5；诊断深度：50（只在漏召回后额外运行，不参与计分）；
- 唯一实验变量：`rerank.enabled: false → true`。

`eval.compare --experiment-variable rerank.enabled` 核对了 suite hash/样本数、KB slug/version/
文档 hash/node 数、embedding、retrieval 配置和实现指纹共 9 项不变量。

## 3. 选参过程

| 变体 | 观察 | 决定 |
|---|---|---|
| 每文档硬 cap `1…5` | 证据经常是正确文档内较后的 chunk；cap=5 仍把 Recall 降到 88.64% | 拒绝 |
| dense/BM25 候选倍数 `1/1…3/2` | `2/2` 的 Top-10 nDCG 最好；加大 BM25 会造成漏召回 | 保留 `2/2` |
| RRF 词法权重 `0…2` | 等权最好；过度加词法权重会丢 Recall | 保留等权 |
| RRF `k=1/10/30/60/100` | `30…100` 几乎等价，`60` 在本集上最好 | 保留 `60` |
| 精排候选文本 | 纯 `content` 的 Top-10 nDCG 83.66%，优于 `title + content` 的 80.93% | 采纳 `content` |
| RRF Top-5 直接精排 | 只改排序，Recall 仍为 86.36% | 候选池放大到 10 |
| RRF Top-10 精排到 Top-5 | Recall 93.18%，nDCG 81.05%，本机额外约 0.7s | 采纳 |

以上是同集探索性选参，不对每一行单独做显著性声称。最终候选才与对照用固定配置重跑严格配对。

## 4. 最终配对结果

| 指标（Top-5） | RRF 对照 | 精排候选 | Δ | 95% CI / 判定 |
|---|---:|---:|---:|---|
| span Recall | 86.36% | 93.18% | +6.82pt | `[-9.09, +22.92]pt`，不显著 |
| nDCG | 61.63% | 81.05% | **+19.42pt** | `[+4.22, +34.39]pt`，**显著提升** |
| MRR | 52.80% | 78.03% | **+25.23pt** | `[+7.14, +43.17]pt`，**显著提升** |
| context precision | 18.18% | 20.00% | +1.82pt | `[-1.74, +5.45]pt`，不显著 |
| 检索上下文 token | 2,243 | 2,194 | -50 | `[-132, +27]`，不显著 |
| 平均检索延迟 | 271.6ms | 926.9ms | **+655.3ms** | `[+567.4, +709.1]ms`，显著变慢 |

nDCG 逐样本为 13 条变好、3 条变差、6 条持平（另 4 条不可答不适用）。
`exact_identifier` 的 Recall 从 77.78% 到 100%，是主要收益来源；`semantic_single_hop`
反而从 100% 到 90.91%，其中 `rag-dev-010` 的 gold 被精排挤出 Top-5。这条回归必须保留为
下轮 holdout 验收和组合打分的 bad case，不用 dev 上的又一个补丁把它“修好”。

## 5. 工程落地

- `search_index` 在精排开启时保留 RRF Top-10，再返回精排 Top-K；
- 请求只携带 chunk `content`，单条最多 1,200 字符，超时 3s；
- 对返回的 candidate id/数量和 finite score 做完整校验；任一错误回退 RRF；
- `KbHit.score_source` 显式标记本条分数来自 dense / lexical / fusion / rerank；
- 评测器分开正式 Top-K 与漏召回诊断 K，并记录 `rerank_applied_count`；
- 仓库默认 `RERANK_ENABLED=false`。当前开发机 `.env` 显式打开；应用进程需重启后才会重读。

## 6. 可复现产物

- RRF 对照：[`report.md`](../../eval/outputs/kb-retrieval/20260823T102607Z-e3-top5-rrf-control-v3/report.md)；
- 精排候选：[`report.md`](../../eval/outputs/kb-retrieval/20260823T102629Z-e3-top5-rerank10-candidate-v3/report.md)；
- 严格配对：[`report.md`](../../eval/outputs/kb-retrieval-compare/e3-top5-rrf-vs-rerank10-v3/report.md)。

两臂 `git_sha=a453d6b14dfd2b34bc9d1a17bf28db1f3c81924c`；对照
`config_hash=8c82787da8fb0bbeb0aa570dac2edddd21049375bffc0732b6fd2a4446a3539b`，候选
`config_hash=95ae4e1ac9a77e74438f4bb114955f93a80df8fe9b3a3fdd179b75720a0def84`。
两臂的 `implementation_fingerprint`均为
`d57e9104fa9bf34aac0fd48f9b3328a1efb2321d0dc690234f69cd3a9f3aecc1`。

## 7. 结论边界与下一步

1. owner 复核这 26 条的问法、fact group 和 quote anchor，再将 suite 升级为 human；
2. 另建 holdout，不在当前 dev 上继续追逐 `rag-dev-010`；
3. 如果要发布默认开启，先把 reranker 模型的下载、启动、健康检查、资源占用与退出纳入 desktop sidecar 生命周期；
4. 扩集后重测延迟/吞吐；当前数字是同机串行 MPS，不外推到其他设备。
