# backend

FastAPI 服务。目录职责见 [CLAUDE.md](../CLAUDE.md)、[docs/02-架构设计.md](../docs/02-架构设计.md)。

W1 建立：模型网关 → 入库流水线 → 检索 → API。
**网关先于业务代码**，否则后期改不动（CLAUDE.md 约束 1）。

## 本地启动

```bash
cd ../deploy && docker compose up -d
cd ../backend
uv sync
uv run alembic upgrade head
uv run fastapi dev app/main.py
```

健康检查：`GET /health/live` 只检查进程，`GET /health/ready` 同时检查 PostgreSQL 与 Redis。

## 质量检查

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy app
uv run pytest
```

测试数据库由 Compose 首次初始化为 `workpilot_test`；测试夹具会拒绝连接名称不以
`_test` 结尾的数据库，防止误清理开发数据。
