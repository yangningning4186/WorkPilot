# Dense-only 工程烟测（2026-08-14）

## 结论

本地 gold span → 当前 heading chunk 区间映射 → bge-m3 query embedding → pgvector dense 检索 →
检索指标与拒答阈值分析的整条链路已跑通。12 条基于已入库文档标题生成的可答题全部在 Top 10
覆盖 gold span；3 条对抗不可答题表明工程初值 `0.35` 过低，会全部放行。该数据集是工程 smoke，
只证明实现可运行，不证明真实问答质量，也不用于确定线上阈值。

原始资料全程只读；逐样本问题、引用文字、文件名和机器报告仅保存在本地数据库及 Git 忽略目录，
本文只记录聚合数字。

## 环境与口径

- 语料：本地已索引的 12 份真实 Markdown/PDF；未复制、上传或改写原文件。
- Embedding：Ollama `bge-m3:latest`，1024 维，revision 固定为当前本地权重摘要。
- 样本：12 条 synthetic title query + 3 条 synthetic unanswerable。
- 检索：heading chunk，dense-only，Top 10，gold span 重叠阈值 θ=0.5，token budget=4000。
- 代码：`69e18ca0ec22862c938a079ade197ee3513d0414`。
- 配置摘要：`e959067bf4b45d3e43f29205b9109e37bc14460ef5db44d46d06fce27e5b185c`。
- 注意：标题题远比真实知识问答简单，且样本量很小。

## 结果

| 指标 | 结果 |
|---|---:|
| span Recall@10 | 1.0000 |
| budget span Recall | 1.0000 |
| nDCG@10 | 0.9218 |
| α-nDCG@10 | 0.9218 |
| MRR | 0.8958 |
| context precision | 0.1000 |
| 拒答 AUROC | 1.0000 |
| 当前阈值 0.35 macro-F1 | 0.4444 |
| 当前阈值误答 / 误拒 | 3 / 0 |
| smoke 最优阈值 | 0.5329 |

首个请求包含本地模型冷启动，15 条平均延迟约 246 ms、p95 约 514 ms；因此本次不把小样本
延迟当性能结论。

## 解读与下一步

1. span Recall@10=1 说明版本/字符区间映射和 dense 查询链路正确工作，不代表复杂问题也能召回。
2. context precision=0.1 与“每题只有一个标题 gold、固定取 10 条”一致，不能据此判断 chunk 冗余。
3. `0.35` 对这三条不可答题全部误答；应保留为工程初值，等至少 20 条人工不可答题后再校准。
4. 下一步用标注工作台完成首批 20 条人工题，覆盖 single-hop、multi-hop、table、unanswerable，
   然后以 `origin=human` 运行同一基线，才登记正式数字。
