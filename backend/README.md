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

## Markdown → dense 最小链路

模型网关统一提供 `complete`、`stream`、`embed`，当前 provider 使用
OpenAI-compatible 的 `/chat/completions` 与 `/embeddings`。聊天模型读取
`TIER_MAIN_BASE_URL` / `TIER_MAIN_MODEL`，embedding 服务优先读取
`EMBEDDING_BASE_URL` / `EMBEDDING_MODEL`（未配置时复用 main 服务）；数据库向量维度由
schema 固定为 1024，`EMBEDDING_DIM` 会在配置加载时强制校验，模型输出也会在写库前校验。

将 Markdown 放进 `LOCAL_LIBRARY_PATH` 后执行：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/documents/ingest-markdown \
  -H 'content-type: application/json' \
  -d '{"path":"notes/retrieval.md"}'

curl -X POST http://127.0.0.1:8000/api/v1/search/dense \
  -H 'content-type: application/json' \
  -d '{"query":"什么是稠密检索？","top_k":5}'

curl -X POST http://127.0.0.1:8000/api/v1/answer \
  -H 'content-type: application/json' \
  -d '{"query":"什么是稠密检索？","top_k":5}'
```

入库接口只接受资料根目录内的 `.md` / `.markdown` 文件，并按 realpath 防止目录穿越。
文本统一为 NFC 与 LF，block offset 使用 Unicode code point；heading 分块、批量 embedding
全部成功后才原子激活新版本。检索只读取当前可搜索版本，并返回 `version_id`、block ID、
字符区间与 heading path，后续引用不需要从 chunk 文本反推来源。每次模型调用会写入
`llm_calls`；原始提示词和文档内容不会写入该审计表。

问答接口把检索结果展开成 block 级 `[S1]` 证据，要求模型逐句引用，并把短标签解析为
`block_id`、`version_id`、原文 quote、字符区间和页面坐标。短标签只在单次消息内有效；
模型引用未知标签或正常回答不带引用时，接口返回 `502 invalid_model_citation`，不会猜测修复。
证据为空或模型明确判断证据不足时返回 `资料库中未找到相关信息。`，且 `refused=true`。

## 质量检查

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy app
uv run pytest
```

测试数据库由 Compose 首次初始化为 `workpilot_test`；测试夹具会拒绝连接名称不以
`_test` 结尾的数据库，防止误清理开发数据。
