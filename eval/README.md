# eval

自建评测框架。设计见 [docs/06-评测体系.md](../docs/06-评测体系.md)、[ADR-0003](../docs/adr/0003-自建评测框架.md)。

与 backend 平级的一等模块，不是测试目录的附属。

## Dense-only 基线

先在本地 `http://127.0.0.1:8000/annotation` 标注 gold spans，再运行：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.dense_baseline \
  --dataset core-dev --origin human --label dense-core-dev-v1 \
  --strategy dense-only \
  --top-k 10 --diagnostic-k 50 --token-budget 4000 --theta 0.5 --alpha 0.5
```

`mapping.py` 只在 `version_id` 相同的前提下计算 gold span 覆盖率；默认重叠阈值 θ=0.5。
`metrics/retrieval.py` 实现 span Recall@K、固定 token budget Recall、nDCG、α-nDCG、MRR 和
context precision；`metrics/refusal.py` 计算 answerable/unanswerable AUROC，并在 dev 样本上扫描
macro-F1 最优阈值。跑批会拒绝 stale span、无 gold span 的可答题，以及 dense-only 不支持的
`global` / `agent_task` 类别。报告还按 category 汇总指标、展示可答/不可答 top score 分布，并将
未命中的 gold span 归因为 token budget 截断、Top-K 外命中、同文档未排入、文档未召回或索引
未覆盖；`--diagnostic-k` 只控制归因深度，不改变正式 Top-K 指标。

同一脚本支持单变量策略对照：

```bash
# 多查询 dense（会把问题文本发送到配置的远端 chat model）
--strategy multi-query-dense

# dense Top-50 → 本地 cross-encoder rerank → Top-K
--strategy dense-rerank

# dense + lexical RRF Top-50 → 本地 cross-encoder → Top-K
--strategy dense-lexical-rrf-rerank

# 完全本地的 dense + lexical + RRF
--strategy dense-lexical-rrf

# 只跑词法单臂。RRF 里 dense 会兜住词法的失效, 只看融合结果无法归因词法打分的好坏
--strategy lexical-only
```

词法打分方式是独立的单变量开关，进 `config_hash`：

```bash
# ts_rank(默认): 分语言 tsvector, english 配置负责停用词与词干, 中文走 bigram
# coverage:      命中词数 / 总词数, 无停用词表、无 IDF、子串匹配(旧默认)
# ts_rank_cd:    同上但用 cover density —— 对中文 bigram 有害, 保留为反例, 见台账 E2
--lexical-mode ts_rank
```

## 数据集

| 名称 | origin | 条数 | 用途 |
|---|---|---:|---|
| `core-dev` | human | 20 | 作者亲手标注的中文与跨语言问题 |
| `core-dev` | synthetic | 6 | hard-negative 候选，不计入 M0 正式 40 条 |
| `multihop-test-v1` | synthetic | 8 | PDF multi-hop 留出集 |
| `english-dev` | human | 20 | AI 构造候选后由 owner 逐条复核升级；补全英文检索盲区（台账 E3） |
| `dense-title-smoke` | synthetic | 15 | 只验工程链路，不作质量结论 |

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.seed_english_dev
```

`english-dev` 的两条有效性前提由种子脚本强制，违反即拒绝入库：问题不含任何 CJK 字符；
问题不与 gold 原文连续重合 4 个词（抄原文会让词法臂靠字面重合命中，测的就不是检索能力）。
种子脚本仍只写入 `synthetic` 候选；不能用脚本自动冒充人工标注。当前 human 身份来自 owner
逐条复核与明确确认，provenance 固化在 `eval/suites/m0-core-40.json`。

## M0 40 条正式套件与生成规则轨

`m0-core-40` 固定组合 `core-dev` 20 条和 `english-dev` 20 条，不复制底层 gold；运行前会校验
数据集条数、14/8/10/8 类别分布、origin、stale span 与可答题 gold 完整性。

```bash
# 生成 + 拒答 + citation_validity + constraint_pass；M0 固定 dense-only
PYTHONPATH=backend backend/.venv/bin/python -m eval.generation_baseline \
  --dataset core-dev --origin human --label M0-formal-generation-core --top-k 5

# 两个 dataset 各跑一次后，合并检索与生成 report.json
PYTHONPATH=backend backend/.venv/bin/python -m eval.m0_report \
  --retrieval-report /path/to/core-retrieval.json \
  --retrieval-report /path/to/english-retrieval.json \
  --generation-report /path/to/core-generation.json \
  --generation-report /path/to/english-generation.json \
  --output-dir eval/outputs/m0-baseline/<run>
```

`citation_validity` 只校验引用格式、记录映射、block/version/document、字符区间和 quote 原文；
语义 `citation_accuracy` 必须填写 `citation-review.csv` 的 `supported`、`reason`、`reviewer`、
`reviewed_at` 后重新合并报告，未覆盖全部实际引用时保持 `pending_human_review`。

完整复核后先做只读校验，再显式写回 `eval_results.human_label` 和 `eval_runs.metrics`：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.import_citation_review \
  --generation-report /path/to/core-generation.json \
  --generation-report /path/to/english-generation.json \
  --review-csv /path/to/citation-review.csv

# 上一步通过后追加 --apply
```

**动词法检索、分词或查询改写的实验，必须同时报中文集与英文集**——
E2 的教训是全中文题集会把英文失效整个藏起来。

远端策略运行前必须确认数据外发范围与目标端点。正式单变量对照应保持 dataset、Top-K、
diagnostic-K、token budget、embedding identity 和 gold span 不变。

带 rerank 的策略需要先启动本机 cross-encoder 服务（见 [reranker/README.md](../reranker/README.md)）；
服务不可用时跑批直接失败，不会静默退回原顺序，避免把降级结果记成实验数字。

送给 cross-encoder 的候选文本可作为单变量切换，用于复现 D6 的三档对照：

```bash
--rerank-candidate-text-mode title_heading_content   # 默认, D6 判定最优
--rerank-candidate-text-mode heading_content
--rerank-candidate-text-mode content
```

## 独立留出集

`multihop-test-v1` 是与 `core-dev` 不重叠的 PDF multi-hop 留出集，只用来验收调参后的策略，
不参与阈值和策略选择：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.seed_multihop_test
PYTHONPATH=backend backend/.venv/bin/python -m eval.dense_baseline \
  --dataset multihop-test-v1 --origin synthetic --label holdout-rrf-rerank-v1 \
  --strategy dense-lexical-rrf-rerank --top-k 10 --diagnostic-k 50
```

## 精排延迟

单独测量 `/v1/rerank` 往返，候选检索不计入耗时；候选数按逗号分隔可一次扫多档：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.reranker_latency \
  --dataset multihop-test-v1 --label rerank-latency-v1 \
  --candidate-counts 10,25,50 --top-k 5 --repeat 3 --warmup 2
```

报告写入 Git 忽略的 `eval/outputs/reranker-latency/`，包含服务侧 device/dtype/batch 配置。

工程链路可用合成 title smoke 检查，但其数字不能作为模型质量结论：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.seed_title_smoke
PYTHONPATH=backend backend/.venv/bin/python -m eval.dense_baseline \
  --dataset dense-title-smoke --origin synthetic --label dense-title-smoke-v1
```

报告保存在 Git 忽略的 `eval/outputs/dense-baseline/`，聚合与逐样本结果同时写入 PostgreSQL。
下一阶段的 Judge 校准和 CI gate 尚未实现。

## 两次跑批的配对对照

`compare.py` 只读两份 `report.json`，不连数据库、不调模型、不重跑检索；
`stats.py` 提供逐样本 paired bootstrap（[docs/06 §4.3](../docs/06-评测体系.md)）：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.compare \
  eval/outputs/dense-baseline/<baseline-run> \
  eval/outputs/dense-baseline/<candidate-run> \
  --output-dir eval/outputs/compare/<label>
```

位置参数可以是 `report.json` 本身，也可以是它所在的跑批目录。支持 `dense_baseline`
（检索轨）与 `generation_baseline`（生成轨）两类报告；`refusal_baseline` 报告没有
`item_id`，无法配对，跑批会直接拒绝。

配对与兼容性校验在计算任何指标之前执行，任一条不满足即拒绝出报告：

- 两份报告必须是同一 `dataset`、同一类型，且 `item_id` 集合完全相同；
- 逐条比对 `category` 与 `answerable`，不一致说明标注已漂移，配对无效；
- 受控配置项（检索轨 `origin` / `top_k` / `token_budget` / `theta` / `alpha`）不同则
  两侧算的不是同一个指标，默认拒绝，确属有意对照时用 `--allow-config-drift` 放行；
- 其余配置差异全部列进报告的「配置差异」表——那才是被对照的实验变量。

统计口径：配对百分位 bootstrap，默认 `--resamples 10000`、`--seed 12345`、`--ci-level 0.95`，
同一份输入永远给出同一个区间。所有指标共用同一批重采样下标，因此指标之间的区间可以并排解读；
类别切片在切片内部单独重采样。**置信区间跨 0 就是"无显著差异"，不允许写成提升**。

逐样本表按主指标（检索轨默认 `budget_span_recall`，生成轨默认 `constraint_pass`，
可用 `--primary-metric` 覆盖）给出变好 / 变差 / 持平 / 不适用四类，`--top-n` 控制
markdown 里各列几条，`report.json` 始终保留全部逐样本差值。

一个样本只要在任一侧不适用某指标（不可答题没有检索指标、一侧拒答另一侧作答、
一侧跑批报错），两侧就一起剔除，剔除数量记在「仅一侧适用」列——
否则比较的是两批不同的样本。因此对照报告里的绝对值可能与单次跑批报告的聚合值不同。

组合拒答跑批只执行“检索分数 + margin + 证据充分性”, 不生成最终答案：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.refusal_baseline \
  --dataset core-dev --origin all --label composite-refusal-v1 \
  --strategy dense-lexical-rrf --top-k 5
```

报告写入 `eval/outputs/refusal-baseline/`, 包含误答、误拒、macro-F1、分类拒答率、非法门控响应和
逐样本原因。该命令会向 chat model 发送问题与截断候选证据, 运行前同样必须确认外发授权。

## PDF 解析质量

对资料根目录中的 PDF 分别跑 PyMuPDF 基线与 MinerU，汇总 block 类型、字符量、结构质量、
耗时和回退情况，并为结构化解析抽样生成 bbox 叠图：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.pdf_parsing_quality \
  --library-root /absolute/path/to/read-only-library --sample-pages 2
```

原始 PDF 只读；报告与叠图写入被 Git 忽略的 `eval/outputs/pdf-parsing-quality/`。人工结论另存
`docs/experiments/`，只记录聚合指标和文件相对名，不复制原文或原始资料。
