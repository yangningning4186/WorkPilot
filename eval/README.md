# eval

自建评测框架。设计见 [docs/06-评测体系.md](../docs/06-评测体系.md)、[ADR-0003](../docs/adr/0003-自建评测框架.md)。

与 backend 平级的一等模块，不是测试目录的附属。

## Dense-only 基线

先在本地 `http://127.0.0.1:8000/annotation` 标注 gold spans，再运行：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.dense_baseline \
  --dataset core-dev --origin human --label dense-core-dev-v1 \
  --top-k 10 --token-budget 4000 --theta 0.5 --alpha 0.5
```

`mapping.py` 只在 `version_id` 相同的前提下计算 gold span 覆盖率；默认重叠阈值 θ=0.5。
`metrics/retrieval.py` 实现 span Recall@K、固定 token budget Recall、nDCG、α-nDCG、MRR 和
context precision；`metrics/refusal.py` 计算 answerable/unanswerable AUROC，并在 dev 样本上扫描
macro-F1 最优阈值。跑批会拒绝 stale span、无 gold span 的可答题，以及 dense-only 不支持的
`global` / `agent_task` 类别。

工程链路可用合成 title smoke 检查，但其数字不能作为模型质量结论：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.seed_title_smoke
PYTHONPATH=backend backend/.venv/bin/python -m eval.dense_baseline \
  --dataset dense-title-smoke --origin synthetic --label dense-title-smoke-v1
```

报告保存在 Git 忽略的 `eval/outputs/dense-baseline/`，聚合与逐样本结果同时写入 PostgreSQL。
下一阶段的 compare、Judge 校准和 CI gate 尚未实现。

## PDF 解析质量

对资料根目录中的 PDF 分别跑 PyMuPDF 基线与 MinerU，汇总 block 类型、字符量、结构质量、
耗时和回退情况，并为结构化解析抽样生成 bbox 叠图：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.pdf_parsing_quality \
  --library-root /absolute/path/to/read-only-library --sample-pages 2
```

原始 PDF 只读；报告与叠图写入被 Git 忽略的 `eval/outputs/pdf-parsing-quality/`。人工结论另存
`docs/experiments/`，只记录聚合指标和文件相对名，不复制原文或原始资料。
