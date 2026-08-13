# eval

自建评测框架。设计见 [docs/06-评测体系.md](../docs/06-评测体系.md)、[ADR-0003](../docs/adr/0003-自建评测框架.md)。

与 backend 平级的一等模块，不是测试目录的附属。

```bash
uv run python -m eval.run --dataset core-dev --label exp-hybrid-rrf
uv run python -m eval.compare baseline exp-hybrid-rrf
uv run python -m eval.calibrate --judge heavy --human-labels labels/round1.yaml
uv run python -m eval.gate --against main
```
