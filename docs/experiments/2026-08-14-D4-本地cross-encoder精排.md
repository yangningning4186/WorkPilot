# D4 · 本地 cross-encoder 精排（2026-08-14）

## 假设

D3 记录了两件事：远端 listwise rerank 把 Recall@10 从 0.7396 抬到 0.8333，证明"精排的质量
假设成立"；但 18.1s 平均延迟让它只能当离线基线，结论是"线上若继续采用 rerank，应换专用
cross-encoder 服务并重新测延迟"。

本次假设：**用本机 `bge-reranker-v2-m3` 替换 35B 生成模型做精排，能同时拿到 D3 的质量收益
和一个数量级的延迟下降；multi-hop 是主要受益类别。**

预注册的证伪条件：如果 Top-50 精排延迟仍在 10s 量级，或 core-dev 上 Δ Recall 的 95% 置信
区间跨 0 且留出集也无提升，则判定"专用 reranker 同样不适合在线"，与 D3 一起记为负结果。

## 结论

- 延迟假设成立：Top-50 精排从 18.1s 降到 3.89s（p95 4.29s），降到约 1/4.7。
- 质量假设**部分**成立：独立留出集上 Recall@10 从 0.6250 涨到 0.9375，配对 bootstrap
  Δ=+0.3125、95%CI=[+0.0625,+0.6250]，不跨 0；但 core-dev 上 Δ=+0.0625、
  95%CI=[+0.0000,+0.1875]，**按台账铁律判为无显著差异**——26 条里只有 1 条变好。
- 两个数据集加起来 24 条可答题，**没有任何一条变差**。可以说非劣，不能说普遍提升。
- 组合拒答**没有收益**：误答仍是 0、误拒仍是 4，macro-F1 完全持平 0.8452，只是把误拒的
  组成换了一条。精排改善的是"证据排得进 Top-K"，没有改善"门控敢不敢答"。
- 负结果：把服务端 batch 从 4 调到 16，Top-50 延迟反而从 3891.6ms 涨到 4175.9ms。
  MPS 上是算力瓶颈不是批处理瓶颈，加大 batch 只会增加 padding 浪费。保留 batch=4。

原始资料只读，没有上传或改写。精排全程在 127.0.0.1:8011，不出本机；组合拒答的证据门控
仍按既有授权向 `172.16.6.66:3270` 发送问题与截断候选。逐样本内容只留在 Git 忽略的本地报告。

## 环境与口径

- 精排模型：`BAAI/bge-reranker-v2-m3`，snapshot `953dc6f6`，权重 sha256 `d9e3e081…5286`
  与 HF 官方元数据核对一致；device `mps`、dtype `float16`、batch 4、max_length 512。
- Embedding：本地 `bge-m3:latest`，1024 维，与 D1–D3 同一 revision。
- 证据门控模型：`qwen3.6-35b-a3b`，thinking 关闭。
- 检索口径：Top 10，诊断 Top 50，token budget 4000，θ=0.5；拒答口径 Top 5。
- 候选正文截断 1200 字符（D3 远端方案是 600，cross-encoder 不受 prompt 预算约束）。
- 代码：`793bbcb8657e`。
- 单变量：两条对照只切换 `dense-lexical-rrf` 与 `dense-lexical-rrf-rerank`，其余全部固定。

`multihop-test-v1` 是本次新建的独立留出集：8 题、16 个 gold span，只取 MemEvolve 与
SWARMRESEARCH 两篇 PDF，与 core-dev 语料不重叠。题目 AI 辅助构造后按激活解析版本原文
逐条校验字符区间。**它是 synthetic，不能替代人工集，只用来验收"调参没有过拟合 core-dev"。**

## 精排延迟

只计 `/v1/rerank` 往返，候选检索不计入。8 题 × 重复 3 次，2 次 warmup。

| 候选数 | 样本 | mean | p50 | p95 | 每候选 |
|---:|---:|---:|---:|---:|---:|
| 10 | 24 | 758.4ms | 826.7ms | 894.5ms | 75.8ms |
| 25 | 24 | 1988.7ms | 2046.1ms | 2199.9ms | 79.5ms |
| 50 | 24 | 3891.6ms | 3856.0ms | 4291.1ms | 77.8ms |
| 50（batch 16） | 24 | 4175.9ms | 4232.7ms | 4345.0ms | 83.5ms |

每候选耗时在三档上几乎不变（75.8 / 79.5 / 77.8 ms），说明延迟对候选数是线性的，
没有可利用的批处理规模效应。**因此"砍候选数换延迟"是唯一的降延迟手段**，
但下一节说明这条路走不通。

## 检索单变量结果

core-dev（20 条人工题，16 可答）：

| 策略 | Recall@10 | multi_hop | single_hop | table | nDCG@10 | MRR | mean latency |
|---|---:|---:|---:|---:|---:|---:|---:|
| dense + lexical + RRF | 0.8646 | 0.4583 | 1.0000 | 1.0000 | 0.7201 | 0.7094 | 271.9ms |
| + 本地 cross-encoder | **0.9271** | **0.7083** | 1.0000 | 1.0000 | **0.8211** | **0.8021** | 4463.1ms |

`multihop-test-v1` 留出集（8 条 multi-hop，16 gold span）：

| 策略 | Recall@10 | nDCG@10 | MRR | mean latency |
|---|---:|---:|---:|---:|
| dense + lexical + RRF | 0.6250 | 0.3481 | 0.2626 | 348.5ms |
| + 本地 cross-encoder | **0.9375** | **0.8277** | **0.8333** | 4473.0ms |

配对 bootstrap（逐样本 Δ span Recall@10，重采样 10000 次）：

| 数据集 | n | Δ | 95% CI | 判定 | 变好/变差/持平 |
|---|---:|---:|---|---|---|
| core-dev | 16 | +0.0625 | [+0.0000, +0.1875] | 跨 0，无显著差异 | 1 / 0 / 15 |
| multihop-test-v1 | 8 | +0.3125 | [+0.0625, +0.6250] | 不跨 0，显著 | 3 / 0 / 5 |

n=8 的 bootstrap 置信区间本身很宽且不稳，"显著"只能当方向性证据，不能当效应量估计。

## 归因

core-dev 之所以只动了 1 条：single_hop 与 table 在 D2 之后已经是 1.0000，没有提升空间，
Recall 的天花板全部压在 4 条 multi-hop 上，分母太小。留出集全是 multi-hop，才把差距露出来。
**这说明 D1–D3 用 core-dev 得到的"整体 Recall"已经饱和，继续用它做策略选择会看不见差异。**

精排回收的证据原本排在 RRF 的第 12、14、37、39、41 位。这解释了为什么不能靠砍候选数降延迟：
把 `RERANK_CANDIDATE_K` 从 50 降到 20，rank 37/39/41 三条证据直接丢失，正好是留出集
3 条变好样本中的 2 条。**延迟和这次的质量收益是绑死的，没有中间档。**

精排后 core-dev 的 budget recall 与 RRF 持平（都是 0.9271），即 Top-50 内可捞的证据已经
全部捞进 Top-10；剩下 3 条 `relevant_chunk_not_ranked`（best overlap = 0）是分块和召回的
天花板，不是排序问题，精排再强也够不到。留出集出现 1 条 `outside_token_budget`：证据排到了
第 10 位但被 4000 token 预算截断——这是新出现的、属于打包而不属于排序的漏洞。

组合拒答持平但组成变了：精排修好了 multi_hop 的"WorkPilot 四个产品场景"，同时新引入
single_hop 的"DeepSeek V4 怎么开启推理模式"误拒。Top-5 下精排把同文档的其他相关块顶了上来，
挤掉了原本命中的那一块。**这是 Top-5 太窄导致的，不是精排质量问题，但它抵消了收益。**

## 决策

1. **`RERANK_ENABLED` 保持 `false`，这个默认值留给人来定。** 依据不是质量而是取舍：
   核心链路延迟从 272ms 涨到 4463ms（约 16 倍），换来的是 multi-hop 的显著提升和其他类别的
   完全持平。CLAUDE.md 把检索融合策略列为"必须自己想清楚"，这里只给数据不替你翻开关。
   翻开关的条件建议是：接受交互式问答 4s 级检索延迟，或先做异步/流式精排。
2. 保留 `dense-lexical-rrf-rerank` 作为可复现的离线策略，并作为 multi-hop badcase 的诊断工具。
3. `RERANKER_BATCH_SIZE` 保持 4，batch 16 已验证为负优化。
4. 后续 core-dev 扩样必须**加 multi-hop 题**，否则整体 Recall 已经没有分辨力。
5. 新增待办：Top-K 打包的 token budget 截断（留出集那条 `outside_token_budget`），
   以及 Top-5 拒答口径下"同文档相关块互相挤占"的问题，都不在精排的职责里，单独开条目。

## 追溯

- 精排延迟：`D4-rerank-latency-v1`、`D4-rerank-latency-batch16`
- core-dev 对照：`D4-core-rrf-baseline-v1`（`27ea709e34502070`）、
  `D4-core-rrf-rerank-v1`（`8962b71b93af0b8e`）
- 留出集对照：`D4-holdout-rrf-baseline-v1`（`ddab73749b53ea84`）、
  `D4-holdout-rrf-rerank-v1`（`6da8df177bf9d139`）
- 组合拒答对照：`D4-refusal-rrf-v1`（`4b9a4cf828971214`）、
  `D4-refusal-rrf-rerank-v1`（`b5364b20487cf4fc`）
- 原始报告：`eval/outputs/dense-baseline/`、`eval/outputs/refusal-baseline/`、
  `eval/outputs/reranker-latency/`（均 Git 忽略）
