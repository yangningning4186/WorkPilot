# eval

自建评测框架。设计见 [docs/06-评测体系.md](../docs/06-评测体系.md)、[ADR-0003](../docs/adr/0003-自建评测框架.md)。

与 backend 平级的一等模块，不是测试目录的附属。

```bash
uv run python -m eval.run --dataset core-dev --label exp-hybrid-rrf
uv run python -m eval.compare baseline exp-hybrid-rrf
uv run python -m eval.calibrate --judge heavy --human-labels labels/round1.yaml
uv run python -m eval.gate --against main
```

## PDF 解析质量

对资料根目录中的 PDF 分别跑 PyMuPDF 基线与 MinerU，汇总 block 类型、字符量、结构质量、
耗时和回退情况，并为结构化解析抽样生成 bbox 叠图：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.pdf_parsing_quality \
  --library-root /absolute/path/to/read-only-library --sample-pages 2
```

原始 PDF 只读；报告与叠图写入被 Git 忽略的 `eval/outputs/pdf-parsing-quality/`。人工结论另存
`docs/experiments/`，只记录聚合指标和文件相对名，不复制原文或原始资料。
