# D1 · Dense-only 首版人工基线（2026-08-14）

## 结论

20 条人工标注表明，当前 dense-only 已能稳定覆盖 single-hop 与 table 的大部分证据，但
multi-hop 是首要检索短板：其 span Recall@10 仅 0.4583。25 个 gold span 均能映射到已索引
chunk，9 个未命中 span 中 6 个在 Top 50 内仍没有排到相关 chunk，说明当前瓶颈主要是排序与
复杂查询表达，而不是解析、索引覆盖或 token budget。

工程阈值 `0.35` 放行了全部 4 条不可答题。可答与不可答 top score 存在重叠，因此不能只把
阈值调高后就宣称拒答问题已经解决；应先扩充 hard-negative，再验证“检索分数 + 证据充分性”
的组合拒答。

原始资料全程只读；逐样本问题、引用文字、文件名和机器报告仅保存在本地数据库及 Git 忽略目录，
本文只记录聚合结果。

## 环境与口径

- 数据集：`core-dev`，20 条 `human` / `valid`；16 条可答，4 条不可答。
- 分类：single-hop 7、multi-hop 4、table 5、unanswerable 4。
- Embedding：Ollama `bge-m3:latest`，1024 维，revision
  `sha256-daec91ffb5dd0c27411bd71f29932917c49cf529a641d0168496c3a501e3062c`。
- 检索：heading chunk，dense-only，正式指标 Top 10，漏召回诊断 Top 50，token budget=4000，
  gold span 重叠阈值 θ=0.5，α=0.5。
- 拒答：当前阈值 0.35；dev 阈值扫描以 macro-F1 为目标。
- 代码：`ec4392a187473a69464ef8e3df9732043c011175`。
- 配置摘要：`79237501f2d3f11c3cb87956654a39a29d7aaf02ea03f3f18ae6b9b85cf66834`。
- Run ID：`019ffd4a-0ee6-741e-bab6-94964608e0d3`。
- 本地原始报告：`eval/outputs/dense-baseline/20260813T224201Z-dense-core-dev-v0/`。

## 总体结果

检索指标按可答题逐题宏平均；因此它与下方 25 个 span 的微观状态计数不是同一分母。

| 指标 | 结果 |
|---|---:|
| span Recall@10 | 0.7396 |
| budget span Recall | 0.8021 |
| nDCG@10 | 0.5879 |
| α-nDCG@10 | 0.5879 |
| MRR | 0.5885 |
| context precision | 0.0813 |
| 平均检索延迟 | 240.8 ms |
| p95 检索延迟 | 248.6 ms |

## 分类别结果

| category | n | span Recall@10 | budget recall | nDCG@10 | MRR | top score median |
|---|---:|---:|---:|---:|---:|---:|
| single_hop | 7 | 0.8571 | 1.0000 | 0.5802 | 0.4881 | 0.6065 |
| multi_hop | 4 | 0.4583 | 0.4583 | 0.5206 | 0.7500 | 0.6602 |
| table | 5 | 0.8000 | 0.8000 | 0.6524 | 0.6000 | 0.6870 |
| unanswerable | 4 | — | — | — | — | 0.5654 |

multi-hop 的 MRR 看似较高，是因为它只衡量第一个相关结果的位置，不能反映一个问题所需多段
证据是否收齐；该类别应以 span recall 为主指标。

## 漏召回诊断

| category | gold spans | Top-10 命中 | Top-10 外、Top-50 内 | Top-50 未排到相关 chunk |
|---|---:|---:|---:|---:|
| single_hop | 9 | 8 | 1 | 0 |
| multi_hop | 11 | 4 | 2 | 5 |
| table | 5 | 4 | 0 | 1 |
| **合计** | **25** | **16** | **3** | **6** |

- `outside_token_budget=0`：当前失败不是 4000 token 预算造成的，不应优先增加上下文长度。
- `no_relevant_indexed_chunk=0`：全部 gold span 都有可映射 chunk，不应先重做解析/分块。
- `document_not_retrieved=0`：失败题仍召回了同版本文档的其他 chunk，主要问题是文档内相关块排序。
- 3 个 span 首次命中排名为 11、15、32；扩大候选集后 rerank 有明确可回收空间。
- 另有 6 个 span 在 Top 50 内仍未命中；只把生成上下文的 `top_k` 从 10 调大无法修复。

## 拒答分数分布

| label | n | min | p25 | median | p75 | p95 | max | mean |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| answerable | 16 | 0.5646 | 0.6029 | 0.6529 | 0.6894 | 0.7405 | 0.8078 | 0.6521 |
| unanswerable | 4 | 0.5385 | 0.5460 | 0.5654 | 0.5855 | 0.5934 | 0.5953 | 0.5661 |

| 拒答配置 | macro-F1 | 误答 | 误拒 | 结论 |
|---|---:|---:|---:|---|
| 当前阈值 0.35 | 0.4444 | 4 | 0 | 4/4 不可答题全部放行 |
| 本 dev 最优阈值 0.5566 | 0.8039 | 2 | 0 | 样本过少，只作方向判断 |

AUROC 为 0.8906，但不可答只有 4 条。不可答最高分 0.5953 高于可答最低分 0.5646，分数区间已经
重叠；单一 dense top score 阈值无法同时做到零误答和零误拒。

## 下一步优化优先级

1. **P0：先补拒答验证集，再改拒答策略。** 将不可答从 4 条补到至少 10 条，优先增加“同主题、
   同实体、库内无答案”的 hard negative。保持当前 0.35 仅作已知工程初值，不把 0.5566 直接
   固化上线；随后对比单分数阈值与“top score + top1/top2 margin + 证据充分性”的组合规则。
2. **P1：针对 multi-hop 做查询分解/多查询召回。** 11 个 multi-hop span 只命中 4 个，且 5 个
   在 Top 50 仍未命中。先把复合问题拆成子问题分别召回再合并，主看 multi-hop span recall，
   其他配置保持不变。
3. **P2：在 Top-50 候选上增加 rerank。** 排名 11、15、32 的 3 个 span 已证明候选深度有可回收
   证据；rerank 后只向生成链路提供较小上下文，避免因盲目增大 top-k 拉低 precision。
4. **P3：再做词法检索 + RRF 对照。** 对专有名词、标题和表格字段预期有效，但需作为单变量实验
   与 dense-only 配对比较，不与查询分解或 rerank 同时上线。
5. **P4：专项复核 1 条 table miss。** table 当前已达 0.8，先检查 Markdown 表格序列化与 query
   embedding，不启动全量 PDF 解析重构。

## 限制与决策

- 本次是第一版人工基线，不是最终 M0 的 40 条基线；分类样本数仍小，不能做显著性声明。
- 尚无独立 `core-test`，所有阈值和优化决策都只能在 dev 上形成假设。
- 本轮只测检索与分数拒答，没有运行生成质量、引用语义支撑或 Judge 指标。
- 采纳本结果作为后续单变量实验的 dense-only 对照；不采纳 dev 最优阈值为线上配置。
