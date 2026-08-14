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
```

远端策略运行前必须确认数据外发范围与目标端点。正式单变量对照应保持 dataset、Top-K、
diagnostic-K、token budget、embedding identity 和 gold span 不变。

带 rerank 的策略需要先启动本机 cross-encoder 服务（见 [reranker/README.md](../reranker/README.md)）；
服务不可用时跑批直接失败，不会静默退回原顺序，避免把降级结果记成实验数字。

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
下一阶段的 compare、Judge 校准和 CI gate 尚未实现。

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
