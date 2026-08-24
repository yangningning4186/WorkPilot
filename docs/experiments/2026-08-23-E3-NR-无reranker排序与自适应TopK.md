# E3-NR：无 reranker 排序与自适应 Top-K

**日期**：2026-08-23

**状态**：完成；两项均未达到改默认门槛

**目标**：在不引入 cross-encoder 的前提下，验证词法权重与确定性自适应深度能否改善产品 Top-5。

## 预注册设计

两组实验都固定 `rag-research:v1`、26 条 synthetic dev、embedding、chunk `512/64`、
dense/BM25 候选倍数 `2/2`、RRF `k=60`、token budget 4000，并强制
`RERANK_ENABLED=false`。当前集仍是 `pending_human_review`，所有结论只用于工程候选。

### NR1：RRF 词法权重

- 对照：dense `1.0` + BM25 `1.0`；
- 候选：dense `1.0` + BM25 `0.75`；
- 两臂都固定 Top-5；
- 主指标：`nDCG@5`；必须同时报 Recall、MRR、context precision、token 和延迟。

采纳标准：`nDCG@5` 配对 bootstrap 95% CI 下界大于 0，且 Recall 点估计不回退。

### NR2：证据不足时扩展 Top-K

当前产品没有独立的确定性语义 evidence gate，因此本实验在看结果前固定一个
**不看 gold** 的双路共识代理信号：

```text
min_score = 1/60 + 1/(60 + 4 - 1) = 0.0325396825
expand = no_hits OR top1_rrf_score < min_score
```

它的含义是：融合第一名还达不到“在一路第 1、另一路第 4”的共识强度时，
再跑一次 Top-10 并向下游交付 10 条；否则仍交付 5 条。该信号只是检索一致性，
不宣称能判断语义上的“证据足够”。

- 对照：固定 Top-5；
- 候选：上述规则在 Top-5 / Top-10 之间选择；
- 主指标：`budget span Recall`；
- 成本指标：扩展题数、不可答题扩展率、检索 token 和延迟。

采纳标准：Recall 方向正向，且 token/延迟增量明显低于全量固定 Top-10；
如 95% CI 跨 0，只记为方向性候选，不改默认。

## 结果

两组严格比较都通过单变量审计。四次跑批的 suite SHA、KB 文档集合、KB index fingerprint、
embedding、rerank 配置和 implementation fingerprint 完全一致；工作树为 dirty，因此另外用
`implementation_fingerprint=70b122ed3f2d` 锚定本次真正参与检索的源码。配对 bootstrap 固定
seed 12345、10,000 次重采样。

### NR1：等权 RRF vs 词法 0.75，Top-5

| 指标 | 等权 RRF | 词法 0.75 | 配对差值与 95% CI | 判定 |
|---|---:|---:|---:|---|
| span Recall@5 | 86.36% | 90.91% | +4.55pp `[0.00, 15.00]` | 不显著 |
| nDCG@5（主指标） | 61.63% | 62.79% | +1.16pp `[-1.64, 5.42]` | 不显著 |
| MRR | 52.80% | 52.95% | +0.15pp `[-2.17, 2.64]` | 不显著 |
| context precision | 18.18% | 19.09% | +0.91pp `[0.00, 3.00]` | 不显著 |
| 平均检索 token/题 | 2243.19 | 2209.42 | -33.77 `[-73.58, 1.42]` | 不显著 |

逐题只有 2 条发生主指标变化：`rag-dev-002` 从漏召回变为命中，
`rag-dev-015` 的证据仍命中但名次下降；因此收益来自 1 条 exact identifier，代价落在
1 条 semantic single-hop。主指标 CI 跨 0，未达到预注册采纳标准。

报告：

- [等权 Top-5 对照](../../eval/outputs/kb-retrieval/20260823T105702Z-e3-nr1-rrf-equal-top5-control-v1/report.md)
  `run=8b4d0489`、`config=5c233dbc`；
- [词法 0.75 Top-5 候选](../../eval/outputs/kb-retrieval/20260823T105736Z-e3-nr1-lex075-top5-candidate-v1/report.md)
  `run=73f977f8`、`config=e0a33eca`；
- [严格配对报告](../../eval/outputs/kb-retrieval-compare/e3-nr1-rrf-equal-vs-lex075-top5-v1/report.md)。

### NR2：固定 Top-5 vs 低共识时 Top-10

| 指标 | 固定 Top-5 | 自适应 5/10 | 配对差值与 95% CI | 判定 |
|---|---:|---:|---:|---|
| budget span Recall（主指标） | 86.36% | 95.45% | +9.09pp `[0.00, 22.73]` | 不显著 |
| nDCG | 61.63% | 65.06% | +3.43pp `[0.00, 8.47]` | 不显著 |
| MRR | 52.80% | 54.21% | +1.41pp `[0.00, 3.51]` | 不显著 |
| context precision | 18.18% | 18.64% | +0.45pp `[-1.74, 3.00]` | 不显著 |
| 平均检索 token/题 | 2243.19 | 2830.35 | +587.15 `[243.46, 982.45]` | 显著增加 |

规则扩展 7/26 题（26.9%）：4 条可答、3 条不可答。它救回了 `rag-dev-002` 和
`rag-dev-021`，但对已经命中的 `rag-dev-003`、`rag-dev-008` 也做了无收益扩展，且没有
触发仍然漏召回的 `rag-dev-001`。因此对 Top-5 三条 miss 的覆盖率是 2/3；按全部扩展计算，
只有 2/7 真正带来 Recall 收益。不可答题扩展率为 3/4，说明“低双路共识”更像歧义/域外信号，
还不能独立充当证据充分性判断。

与成本参照相比，总交付 token 为：固定 Top-5 `58,323`、自适应 `73,589`、固定 Top-10
`113,666`。自适应比 Top-5 增加 26.2%，但比固定 Top-10 少 35.3%；它只消耗了
“Top-5 全量升级 Top-10”额外 token 的 27.6%。代价虽受控，Recall CI 仍包含 0，未达到
预注册改默认门槛。

报告：

- 固定 Top-5 复用 NR1 等权对照；
- [自适应 Top-10 候选](../../eval/outputs/kb-retrieval/20260823T105804Z-e3-nr2-adaptive-top10-candidate-v1/report.md)
  `run=d2d9cb30`、`config=51ca95ab`；
- [固定 Top-10 成本参照](../../eval/outputs/kb-retrieval/20260823T105832Z-e3-nr2-fixed-top10-cost-reference-v1/report.md)
  `run=d85f474c`、`config=e3d44bc3`；
- [严格配对报告](../../eval/outputs/kb-retrieval-compare/e3-nr2-fixed5-vs-adaptive10-v1/report.md)。

## 延迟口径

正式报告的首个对照请求包含约 1.97 秒本地 embedding 冷启动，后续两臂首题只有
0.49--0.63 秒，因此跨跑批均值被运行顺序污染，不能把 NR1 报告中的“显著更快”归因给
词法权重。去掉各跑首题后，等权/词法 0.75 的均值为 214.78/210.61ms，差异很小。

当前自适应实现先取 Top-5，触发后再取 Top-10；7 条触发题相对对照的配对延迟中位数
增加约 209.66ms。固定 Top-10 的中位数为 212.85ms，与固定 Top-5 的 210.71ms 接近，
说明后续若继续实验，应改成“一次取 Top-10，再按共识交付 5/10 条”，避免重复 embedding
和索引查询；本次不能在看过结果后更换实现并沿用同一个预注册结论。

## 结论与落地

1. **默认继续使用等权 RRF + 固定 Top-5，reranker 仍不参与这两组实验。**
2. 词法 0.75 是方向性候选，但只改变两题且主指标不显著，不足以全局降权 BM25；尤其
   semantic single-hop 已出现排序回退。
3. 自适应 Top-K 能以远低于全量 Top-10 的上下文成本救回 2/3 miss，但当前阈值对不可答题
   过度敏感，且二次检索实现浪费延迟，不进入生产默认。
4. 下一轮只在人工复核/扩充后的集合上验证两个更窄的候选：query 类型感知的 lexical
   weight，以及“一次取 10、按 evidence gate 交付 5/10”的自适应上下文；不要继续在这
   26 条 synthetic dev 上扫阈值，否则会直接过拟合。

工程侧保留了可复现实验能力：manifest 可记录 `rrf_lexical_weight`，runner 可声明权重和
自适应参数，compare 会严格审计这两个单变量。生产默认值没有改变。
