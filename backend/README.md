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

## local_dir → PDF/Markdown → dense 最小链路

模型网关统一提供 `complete`、`stream`、`embed`，当前 provider 使用
OpenAI-compatible 的 `/chat/completions` 与 `/embeddings`。聊天模型读取
`TIER_MAIN_BASE_URL` / `TIER_MAIN_MODEL`，embedding 服务优先读取
`EMBEDDING_BASE_URL` / `EMBEDDING_MODEL`（未配置时复用 main 服务）；数据库向量维度由
schema 固定为 1024，`EMBEDDING_DIM` 会在配置加载时强制校验，模型输出也会在写库前校验。
每组向量同时记录 `EMBEDDING_MODEL`、provider 和 `EMBEDDING_REVISION`；查询只会读取完全相同
的向量空间。模型或权重变化后必须更新 revision，同内容文档也会自动建立新版本并重算向量，
旧向量在新版本激活前仍保持可检索。

本机可用 Ollama 启动 E0 候选对照：

```bash
brew install ollama
brew services start ollama
ollama pull bge-m3
ollama pull qwen3-embedding:0.6b
cd ..
uv run --project backend python -m eval.embedding_bakeoff
```

跑批会验证模型清单与 1024 维输出，分别重建隔离的内存索引，并报告 span recall、nDCG、
MRR、固定 context budget recall、拒答 AUROC/阈值和本机吞吐/延迟。原始报告写入忽略提交的
`eval/outputs/embedding-bakeoff/`，人工结论记录在 `docs/experiments/`。

把 `LOCAL_LIBRARY_PATH` 指向资料根目录。注册 `local_dir` source 后，同步会递归扫描
`.md` / `.markdown` / `.pdf`：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/sources/local-dir \
  -H 'content-type: application/json' \
  -d '{"root":".","name":"my-library"}'

curl -X POST http://127.0.0.1:8000/api/v1/sources/<source_id>/sync \
  -H 'content-type: application/json' \
  -d '{"max_chunk_chars":2000}'
```

同步游标持久化为 `size_bytes + mtime_ns + content_hash`。未变文件直接跳过，更新文件建新版本并
在全部成功后激活，消失文件软删除并退出检索。单文件失败只记录该文件，不会阻断整个目录。
隐藏目录、非白名单后缀和符号链接不会入库；注册根目录与每个文件都做 realpath 边界校验。

也可以单独导入一个文件：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/documents/ingest-markdown \
  -H 'content-type: application/json' \
  -d '{"path":"notes/retrieval.md"}'

curl -X POST http://127.0.0.1:8000/api/v1/documents/ingest-pdf \
  -H 'content-type: application/json' \
  -d '{"path":"papers/example.pdf"}'

curl -X POST http://127.0.0.1:8000/api/v1/search/dense \
  -H 'content-type: application/json' \
  -d '{"query":"什么是稠密检索？","top_k":5}'

curl -X POST http://127.0.0.1:8000/api/v1/answer \
  -H 'content-type: application/json' \
  -d '{"query":"什么是稠密检索？","top_k":5}'
```

单文件入库接口只接受资料根目录内的 Markdown/PDF，并按 realpath 防止目录穿越。
文本统一为 NFC 与 LF，block offset 使用 Unicode code point；heading 分块、批量 embedding
全部成功后才原子激活新版本。检索只读取当前可搜索版本，并返回 `version_id`、block ID、
字符区间与 heading path，PDF block 还会返回页码、页面尺寸、旋转角、左上原点及归一化
`bbox_norm`，后续引用不需要从 chunk 文本反推来源。每次模型调用会写入
`llm_calls`；原始提示词和文档内容不会写入该审计表。

当前 PDF 基线使用 PyMuPDF 文本层，在独立子进程中执行，有文件大小、页数、CPU、内存和超时护栏；
已做基础双栏排序与重复页眉/页脚过滤。扫描件 OCR、表格还原、公式 LaTeX 和更强的版面语义留给
MinerU 阶段；无文本层 PDF 会明确失败，不会产生空文档。

问答接口把检索结果展开成 block 级 `[S1]` 证据，要求模型逐句引用，并把短标签解析为
`block_id`、`version_id`、原文 quote、字符区间和页面坐标。短标签只在单次消息内有效；
模型引用未知标签或正常回答不带引用时，接口返回 `502 invalid_model_citation`，不会猜测修复。
证据为空或模型明确判断证据不足时返回 `资料库中未找到相关信息。`，且 `refused=true`。

生成前还有一层确定性拒答门控：最高 dense score 小于 `REFUSAL_THRESHOLD` 时直接拒答，
不会调用生成模型。响应同时返回 `top_score`、`threshold` 和 `refusal_reason`；其中
`below_threshold` 表示低分拒答，`no_evidence` 表示没有检索证据，
`model_insufficient_evidence` 表示通过门控后模型仍判断证据不足。默认阈值 `0.35` 只是
embedding 服务上线前的工程初值，不能当成质量结论；接入真实模型后必须用
answerable / unanswerable 标注集做 ROC 与误拒答率校准。

## 质量检查

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy app
uv run pytest
```

测试数据库由 Compose 首次初始化为 `workpilot_test`；测试夹具会拒绝连接名称不以
`_test` 结尾的数据库，防止误清理开发数据。
