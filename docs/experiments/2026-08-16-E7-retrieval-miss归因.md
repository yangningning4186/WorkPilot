# E7 · 11 条 retrieval miss 归因（2026-08-16）

## 结论先行

E5 留下的 11 条 retrieval miss 不是同一个病。按 span 级诊断可以干净地拆成两组，
**两组的修法完全不同，且没有一组能靠加大 `top_k` 解决**：

| 组 | 条数 | 机制 | 加大 top_k 能修吗 |
|---|---:|---|---|
| A 截断损失 | 6 | 正确 chunk **已经排进序列**，名次 15/19/19/26/28/34，被 `top_k=10` 截掉 | 名义上要 K≥35，但见下 |
| B 候选缺失 | 5 | chunk 已索引、同文档其他 chunk 已召回，但该 chunk 在 50 深的排序里**从未出现** | 不能，任何 K 都无效 |

对 A 组，"把 K 调大"是个陷阱：**全部 13 条误拒在 K=10 时 gate 证据就已经占满 6000 字符预算**。
gate 按最终排序顺序连续打包，预算在前十名附近就耗尽，排在第 34 位的 chunk 无论 K 设多大都进不了
门控输入。所以 A 组的真实约束是**排序质量**（正确 chunk 必须挤进前十），不是候选深度。

结论：下一轮不要动 `top_k`，也不要动 gate 阈值。A 组要改排序，B 组要改候选生成。

## 归因口径

- 检索 run：`eval/outputs/dev-suite-retrieval/20260816T020650.974049Z-m1-dev70-finalists-20260816/heading/report.json`
- 归因 run：`eval/outputs/evidence-gate-analysis/m1-dev70-heading-gatefix-final-20260816/report.json`
- 检索配置：`top_k=10`、`diagnostic_k=50`、`token_budget=4000`、`theta=0.5`、
  `rrf_k=60`、rerank `bge-reranker-v2-m3`、lexical `ts_rank`
- gate 配置：`gate_max_chars=6000`、`packing_mode=sequential`、`answer_max_chars=12000`

`diagnose_spans` 的两个状态含义必须分清，它们指向不同的子系统：

- `outside_top_k`：`first_hit_rank` 存在但 > `top_k`。正确 chunk 排进来了，只是名次不够。
- `relevant_chunk_not_ranked`：`first_hit_rank` 为空，但该 chunk 存在于 `candidates`
  且同版本其他 chunk 已被检索。**`candidates` 是直接查库取到的该文档版本全部已索引 chunk**
  （`dense_baseline._candidate_chunks`），不是管线的融合候选池——所以这个状态的含义是
  "chunk 索引完好、文档够得着，但它自己从未进入管线的排序输出"，属于候选生成缺失，
  发生在 rerank 上游。

全 70 条的非 hit span 共 23 个：`outside_top_k` 15 个、`relevant_chunk_not_ranked` 8 个，
`document_not_retrieved` 与 `no_relevant_indexed_chunk` 均为 **0**——语料和索引本身没问题。

## A 组：截断损失（6 条）

全部 15 个 `outside_top_k` span 的 `best_retrieved_overlap` 都是 **1.0**，即 chunk 完整包住
gold span。reranker 找到了正确的块，只是没把它排进前十。名次分布：

```
14 15 16 19 19 20 21 25 26 26 28 30 34 36 43
```

按条聚合，覆盖全部 gold span 所需的最小 K：

| K | 全 span 覆盖的 miss 条数 |
|---:|---:|
| 10 | 0/11 |
| 15 | 1/11 |
| 20 | 3/11 |
| 30 | 5/11 |
| 35 | 6/11 |
| 50 | 6/11 |

K=35 是 A 组的天花板，再往上不再恢复任何一条。而 K=10 时 `context_precision` 已经只有
**0.1035**，检索 token 4211/条；K 提到 35 会把上下文精度进一步稀释约 3.5 倍，同时——如前所述——
gate 的 6000 字符预算早在前十名就耗尽，多出来的候选根本到不了门控模型面前。

**因此 A 组不是"召回深度不够"，是"排序没把对的块放到前面"。** 可查的方向：rerank 的
`title_heading_content` 拼接是否让长 chunk 吃亏、RRF 的 `rrf_k=60` 是否过度压平 dense 与
lexical 的分数差、以及 heading 分块下同文档相邻块的高度相似是否造成互相挤占。

## B 组：候选缺失（5 条）

8 个 `relevant_chunk_not_ranked` span 全部落在 `multi_hop`（4）与 `global`（4），
`best_retrieved_overlap` 全为 **0.0**——不是排低了，是压根没出现。

检索回来的文档构成暴露了机制。这 5 条都是跨文档题，但 top-K 被**单一文档霸榜**：

| 条目 | 需要的文档 | 实际召回构成 |
|---|---|---|
| ASP × Socratic-SWE 的验证盲区 | ASP + Socratic-SWE | Socratic-SWE 28/28，ASP **一条没有** |
| 自演化 coding agent 的共同弱点 | CODESKILL + Socratic-SWE + Self-play | Socratic-SWE 23，CODESKILL 3，MemEvolve 1 |
| 两个 agent benchmark 的规模 | GAIA + AgentBench | Agent-World 20，AgentBench 3，GAIA **一条没有** |
| 两份 7 月笔记治理什么资源 | DGX Spark + 补遗 | VibeCoding 12，DGXSpark 3，其余 2 |
| Self-play SWE-RL × AutoGen 的瓶颈 | Self-play + AutoGen | Socratic-SWE 5，Self-play 2 |

同一主题下语义最接近的那篇论文吃掉几乎全部名额，题目真正需要的第二篇被饿死。这是
**多样性问题**，不是深度问题：RRF 融合与 cross-encoder rerank 都只按单点相关性排序，
没有任何按文档/版本去冗的机制。

值得注意的是评测已经在测 `alpha_ndcg_at_k`（多样性感知指标），本轮它等于 `ndcg_at_k`
（0.7117），说明当前口径下还没把这个维度真正区分出来——这本身是下一步要先修的观测问题。

可查的方向：融合或 rerank 后按 `version_id` 做名额上限（每文档最多 N 块）、
MMR 式去冗、或对已判定为跨文档的问题走子查询分解后按子查询分配名额。

## 一个被证伪的假设

初看归因结果时，被召回文档里频繁出现 `_副本` 后缀（`09-答案库-推理部署与系统设计_副本`、
`11-近三个月新考点补遗_副本` 等），怀疑语料存在重复文档，近似块互相挤占名额并压低
`context_precision`。

**查库证伪**：40 篇活跃文档，标题含"副本"的 13 篇，但按标题归一化后同族计数 >1 的为
**0 篇**。`_副本` 只是作者的文件命名，库里没有重复文档。`context_precision=0.10` 的成因
另有出处，不能记到重复语料头上。

记在这里是因为这个假设如果不查就写进结论，会把下一轮的工作引到完全错误的方向。

## 决策与下一步

- **不调 `top_k`，不调 gate 阈值。** 二者都不是这 11 条的约束点。
- A 组（6 条）：先做排序归因——看正确 chunk 在 rerank 前后的名次变化，判断是融合阶段
  还是 cross-encoder 阶段把它压下去的。
- B 组（5 条）：先修观测（让 `alpha_ndcg` 真正区分多样性），再试每文档名额上限，
  在同一 70 条上做受控对照，不为 5 条样本全局放大上下文。
- 两组都改完之前，不宣称 `global` 0/6 是生成或 Judge 的问题——它目前是纯检索问题。
- 剩余 2 条非 retrieval 成因（1 条 12k answer 证据预算缺第二跳、1 条 SimpleMem 标注缺陷）
  按 E5 决定处理，本轮不动。
- 10 条 test 继续隔离。

## 追溯

- 上游：[E5 · Evidence gate 误拒归因与修复](2026-08-16-E5-evidence-gate误拒修复.md)
- 并行：[E6 · Judge 二分类 rubric 与 70 条草稿标注](2026-08-16-E6-Judge二分类rubric与70条草稿标注.md)
