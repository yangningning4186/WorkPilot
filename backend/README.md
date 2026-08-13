# backend

FastAPI 服务。目录职责见 [CLAUDE.md](../CLAUDE.md)、[docs/02-架构设计.md](../docs/02-架构设计.md)。

W1 建立：模型网关 → 入库流水线 → 检索 → API。
**网关先于业务代码**，否则后期改不动（CLAUDE.md 约束 1）。
