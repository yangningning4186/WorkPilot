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

## API 权限边界

除健康检查和 admin 登录外，API 不再匿名裸奔：

| 表面 | 权限 |
|---|---|
| `/api/v1/runs/**` | 匿名 demo session，只能操作自己的 conversation/run/SSE；固定综述的创建与 resume 额外要求 demo admin |
| `/api/v1/documents/{version_id}/**` | demo session，且该版本必须被自己的 run 引用过 |
| `/api/v1/sources/**`、入库、直接检索、同步回答 | demo admin |
| `/api/v1/annotation/**`、`/annotation` | demo admin；生产环境仍固定关闭标注工具 |
| `/health/**`、`/api/v1/auth/admin/login` | 公开 |

admin 密码只以 bcrypt hash 写入环境变量，会话只存 Redis：

```bash
uv run python -m app.cli.hash_admin_password
# 把输出写入 .env 的 DEMO_ADMIN_PASSWORD_HASH

curl -c /tmp/workpilot-admin.cookie \
  -H 'content-type: application/json' \
  -d '{"password":"<你的密码>"}' \
  http://127.0.0.1:8000/api/v1/auth/admin/login
```

维护接口的后续 curl 请求使用 `-b /tmp/workpilot-admin.cookie`。未配置 hash 时 admin 登录
fail-closed 返回 `503`，不会在开发环境隐式放行。

生产环境默认启用两层滥用防护：Redis 原子 token bucket 按 `request.client.host` 限制每 IP
每分钟 20 次、突发 5 次；PostgreSQL 条件更新限制每个 30 分钟 demo session 最多提问 20 次。
Redis 不可用时请求层 fail-closed 返回 `503`，桶耗尽返回带 `Retry-After` 的 `429`；session
配额在并发请求下也不会穿透。开发/测试默认不启用 IP 限流，可用
`IP_RATE_LIMIT_ENABLED=true` 显式打开。反向代理部署时只应信任已知代理地址，让 Uvicorn
校正 `request.client`，不要直接信任客户端可伪造的 `X-Forwarded-For`。

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

固定综述写回使用独立的 `AGENT_OUTPUT_PATH`，只接受该目录内的相对 `.md` 路径。
只读图生成预览后进入 `waiting_human`；owner 调用 `POST /api/v1/runs/{run_id}/resume`
批准后才执行 `write_note`。写回不会直接触碰 `LOCAL_LIBRARY_PATH`，如需入库由后续同步显式完成。

**批量导入不要走上面的 HTTP 接口。** 本机实测 MinerU 约 166s/篇（7.4s/页），百篇量级要跑数
小时，既超过 Arq 的 `job_timeout`，也违反"worker 不依附 HTTP 连接"。用脱离连接的 CLI，
它复用同一套增量游标与失败隔离，中断后重跑会跳过已入库文件：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m app.cli.ingest_library
PYTHONPATH=backend backend/.venv/bin/python -m app.cli.ingest_library --root papers
```

逐文件打印 `added/updated/skipped/failed` 与耗时，结束后汇总激活文档数与可检索 chunk 数。

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

PDF 解析支持 `auto` / `pymupdf` / `mineru` 三种模式。`auto` 先用轻量 PyMuPDF 分析文本密度、
图片和多栏版面；简单文本直接采用基线，复杂版面转 MinerU。MinerU 输出会归一化为标题、段落、
表格 Markdown、公式 LaTeX、图注等 block，并丢弃页眉、页脚、页码和旁注。所有结果都经过字符区间、
页码、页面尺寸和 bbox 结构校验；MinerU 不可用、超时或质量门控失败时按配置回退 PyMuPDF，并在
`document_versions.parse_meta` 留下实际 parser/backend、选择理由、回退原因、耗时和质量指标。

MinerU 使用独立环境，避免其模型栈污染 FastAPI 依赖。Apple Silicon 本地安装：

```bash
cd ..
uv venv --python 3.12 .mineru/.venv
uv pip install --python .mineru/.venv/bin/python \
  -r requirements-mineru.txt
cp .env.example .env
# 把 PDF_MINERU_COMMAND 改为当前仓库下 .mineru/.venv/bin/mineru 的绝对路径
```

首次 MinerU 解析会自动下载约数 GB 的本地模型，耗时显著高于热启动。模型和 `.mineru/`、原始资料、
质量跑批产物均被 Git 忽略。`PDF_MINERU_TIMEOUT_S` 是整份文档的墙钟护栏；超时或任务取消会终止
整组子进程。解析策略、模型 revision、分块参数或 embedding 身份变化会改变 `ingest_signature`，
下一次 `local_dir` 同步会自动重解析/重向量化，文件未变化也不会误跳过。

真实 PDF 质量跑批会对比 PyMuPDF 与 MinerU，并输出 JSON、Markdown 和抽样 bbox 叠图：

```bash
cd ..
PYTHONPATH=backend backend/.venv/bin/python -m eval.pdf_parsing_quality \
  --library-root /absolute/path/to/read-only-library --sample-pages 2
```

无文本层 PDF 在 PyMuPDF 模式会明确失败；`auto` / `mineru` 可尝试 MinerU OCR。现阶段表格中的
`rowspan` / `colspan` 会按普通单元格展开，跨页表格合并和扫描件专项基线仍需后续语料验证。

问答接口把检索结果展开成 block 级 `[S1]` 证据，要求模型逐句引用，并把短标签解析为
`block_id`、`version_id`、原文 quote、字符区间和页面坐标。短标签只在单次消息内有效；
模型引用未知标签或正常回答不带引用时，接口返回 `502 invalid_model_citation`，不会猜测修复。
证据为空或模型明确判断证据不足时返回 `资料库中未找到相关信息。`，且 `refused=true`。

生成前使用组合拒答：先检查最高检索分数和 top1/top2 margin，再让远端模型严格判断候选证据
是否明确覆盖问题所需的全部事实。证据门控只接收问题与 `EVIDENCE_GATE_MAX_CHARS` 截断后的
候选片段；非法 JSON 会 fail-closed 为 `evidence_gate_invalid`，明确缺证据则返回
`model_insufficient_evidence`。响应同时暴露分数、margin、门控原因和实际模型，便于离线校准。

复杂问题可由远端模型分解为最多 4 个自包含子查询，批量 embedding 后融合召回；规划失败自动
退回原 query。Top-50 候选发送到本机 `RERANKER_BASE_URL` 的专用 cross-encoder 做一次批量精排，
失败时保留原排序，不再用 35B 生成模型做 listwise rerank。门控证据按 Top-K 候选轮询打包，并以
rerank 门控按精排顺序连续打包 block；`RERANK_EVIDENCE_GATE_MAX_CHARS` 控制总预算。
旧的跨候选轮询会只取多个 chunk 的首 block，可能把高排名 chunk 内的关键后续 block 截掉。
`LEXICAL_RRF_ENABLED` 默认开启，会用英文标识符与中文双字词做本地词法召回，再以
RRF 与 dense 合并。查询分解和证据充分性门控仍是远端步骤，只发送问题及截断候选，不上传原始
文件；部署时仍需按数据策略确认具体语料是否允许外发。默认阈值 `0.35` 只是工程初值，不能当成
质量结论。

本地 reranker 使用独立环境，避免主后端引入 PyTorch。首次启动会下载约 2.3GB 权重：

```bash
cd ..
uv sync --project reranker --group dev
HF_HOME="$PWD/.cache/huggingface" \
  uv run --project reranker uvicorn reranker_service.main:app \
  --app-dir reranker --host 127.0.0.1 --port 8011
curl http://127.0.0.1:8011/health
```

## 向量索引：每策略一个部分 HNSW

四种分块策略共用一个 HNSW 再按 `strategy` 过滤是错的——pgvector 在**候选扫描阶段**过滤，
扫出的候选大部分会被丢掉，很可能凑不满 top-k。更要命的是 W3 的四策略对照会把索引退化的
噪声混进结论里。因此每策略一个部分索引（`idx_chunk_vec_{fixed,heading,recursive,semantic}`），
谓词是 `strategy = '<策略>' AND is_searchable`。

**查询必须显式带 `is_searchable = true` 和 `strategy = '...'`，否则命不中部分索引。**
剩余过滤（embedding 身份、`doc_type`）仍在索引内进行，靠迭代扫描兜底：每次检索前用
`SET LOCAL` 设置 `hnsw.iterative_scan` / `hnsw.max_scan_tuples` / `hnsw.ef_search`。
用 `SET LOCAL` 而不是 `SET` 是因为连接是池化的，会话级设置会漏给后续无关查询。
`HNSW_EF_SEARCH` 必须不小于实际 `top_k`，否则召回会被候选队列长度悄悄截断。

M0 只有 `heading` 有数据，其余三个是空分区、不产生写入开销，建在这里是为了 W3 建另外
三套 chunk 时不必再改 schema。

> 一次性导入大量语料时，HNSW 是逐行增量维护的，会明显慢于建好数据再建索引。
> 200 篇规模无所谓；真要批量灌几万 chunk，先 `DROP INDEX` 再重建更快。

## run / SSE：任务不依附 HTTP 连接

普通问答也走 run（[ADR-0007](../docs/adr/0007-agent幂等与事件溯源.md)）。接口只负责创建 run
并入队，执行在 Arq worker 进程；关掉页面任务照跑，刷新回放与实时流读的是同一份
`run_events`，因此不会出现"实时看到的和刷新后看到的不一致"。

公网 demo 使用 30 分钟有效的匿名 `HttpOnly` session cookie。数据库只保存 token 的 SHA-256，并把
conversation 绑定到 session；run 状态、取消和 SSE 都经 conversation 校验所有权。不存在的对象
和其他 session 的对象统一返回 `404`，避免泄漏对象是否存在。生产环境
自动带 `Secure`，浏览器前后端应走同源访问。citation 原文还会校验该版本确实出现在当前
session 自己的 run 事件中，拿到 `version_id` 也不能越权读取。命令行调试必须复用 cookie jar：

worker 需要单独起一个进程：

```bash
uv run arq app.worker.main.WorkerSettings
```

```bash
# 创建 run，立即返回 run_id（202），不等生成
curl -X POST http://127.0.0.1:8000/api/v1/runs \
  -c /tmp/workpilot-demo.cookie \
  -H 'content-type: application/json' \
  -d '{"query":"什么是稠密检索？","top_k":5}'

# 订阅事件流：先补历史，再续实时
curl -N -b /tmp/workpilot-demo.cookie \
  http://127.0.0.1:8000/api/v1/runs/<run_id>/events?after_seq=0

# 断线重连由浏览器自动带 Last-Event-ID，手工模拟：
curl -N -b /tmp/workpilot-demo.cookie -H 'Last-Event-ID: <run_id>:12' \
  http://127.0.0.1:8000/api/v1/runs/<run_id>/events

curl -X POST -b /tmp/workpilot-demo.cookie \
  http://127.0.0.1:8000/api/v1/runs/<run_id>/cancel
```

事件信封与 [docs/08 §3.2](../docs/08-前端设计.md) 一致：

```
id: <run_id>:<seq>
event: message.delta
data: {"id":"<run_id>:<seq>","run_id":"…","seq":"12","type":"message.delta","data":{"text":"…"}}
```

- `seq` 用字符串传，DB 是 BIGINT，直接给 JS number 会丢精度；前端比较时转 `BigInt`。
- SSE 的 `id:` 就是 `run_id:seq`，浏览器 `EventSource` 重连会自动带回 `Last-Event-ID`，
  服务端解析出 seq 续发——这是断线续传（B2）成立的机制。`Last-Event-ID` 优先于 `after_seq`。
- 当前发出的事件类型：`message.start` / `message.delta` / `citation` / `message.done` /
  `error`。`plan` / `step.update` / `interrupt` 随 M1 的 LangGraph 工作流引入。
- `citation` 是独立事件而非嵌在正文里，并携带完整定位元数据（`block_id` / `doc_id` /
  `locations[]` 含页码、页面尺寸、旋转、坐标原点、`bbox_norm`），前端才能跨渲染器正确高亮。
- delta 按 `RUN_DELTA_FLUSH_CHARS` / `RUN_DELTA_FLUSH_MS` 批量落库，结构化事件逐条落；
  写事务提交之后才发唤醒通知，否则订阅方被唤醒却查不到事件。
- 续流服务器**先订阅再查库**，每次醒来和心跳超时都重新查库。通知可以丢，数据库事件不能丢，
  因此不存在"查完历史、订阅生效之前"的丢事件窗口。无事件时发 `: keepalive` 注释帧保活。

run 有 worker 租约与 heartbeat。关闭页面不会取消 run；worker 崩溃后租约过期，watchdog 把
普通流式回答**显式标记为失败**（`messages.status = failed`）并发 `error` 事件，**不自动重试**——
一次已经发出去的模型调用是否计费无法确认，静默重放等于重复计费。能自动恢复的只有带
checkpoint 且工具满足幂等边界的 Agent run。取消对已在执行的 run 只打标记，由持有租约的
worker 在下一个检查点自己收尾，避免"终态之后仍在写事件"。

> 当前 `message.delta` 是整答生成完成后按标点切片产出的，首 token 延迟等于整答延迟。
> 这是为了让检索、拒答阈值、证据充分性门控只有一条实现路径（约束 6）；
> 把 `app/services/answer_stream.py` 的 `produce_answer` 换成 `gateway.stream()` 即可
> 变成真流式，对外协议不变。

## 每日成本硬上限

模型网关在**调用前原子预留、调用后按实际用量结算**（[docs/12 §2.2](../docs/12-安全与部署.md)）。
调用后统计余额会被并发请求一起穿透，所以预留走 `daily_cost_budgets` 的条件 UPDATE。

三条失败路径的语义不同，不能混：

| 情况 | 处理 | 理由 |
|---|---|---|
| 额度不足 | 直接抛错，**不发起调用** | 应用侧唯一能真正控制的就是"是否再发起调用" |
| 确认未发给 provider（建连失败） | 释放全部预留 | 只有建连阶段失败能证明一个字节都没发出去 |
| 结果不明（读超时、进程崩溃） | 保留预留，到期后按上限记为已花费 | 无法证明 provider 没计费；直接释放等于假设它没收钱 |

`PRICE_*_USD_PER_MTOK` **默认为 0**，表示本地自部署模型，此时网关跳过预留直接调用。
换成商用 API 时必须填价格，否则每日上限形同虚设。token 估算默认按 1 字符 = 1 token 的保守
上界，多预留的部分在结算时释放。provider 不返回 usage 时按预留上限记账，不按 0 记。

应用侧无法证明 provider 在网络超时后一定没计费，因此线上仍需配置 provider 账户级月度限额
作为最终保险丝。

## Gold span 标注与 dense-only 基线

数据库迁移完成并启动后端后，打开 `http://127.0.0.1:8000/annotation`。本地工作台可以：

- 按资料、block 类型和正文搜索证据；
- 对 Markdown/PDF 原文拖选，后端把浏览器 UTF-16 offset 转为 Unicode code-point offset；
- 对 PDF 页渲染 bbox 定位预览；
- 标注 single-hop、multi-hop、table、unanswerable，以及答案、约束、难度与来源；
- 保存时从 `document_versions.full_text` 回切 quote；解析版本变化后将旧 span 标为 stale。

页面和 API 受 `ANNOTATION_TOOL_ENABLED` 控制，且 `APP_ENV=production` 时无条件关闭。它只读取
已注册 `local_dir` 中的原文件，不会修改或复制资料。人工标注保存在本地 PostgreSQL。

完成一批人工样本后运行纯 dense 基线：

```bash
cd ..
PYTHONPATH=backend backend/.venv/bin/python -m eval.dense_baseline \
  --dataset core-dev --origin human --label dense-core-dev-v1 \
  --top-k 10 --token-budget 4000
```

独立 PDF multi-hop 留出集可按当前激活解析版本重新生成；脚本只写评测表，不修改资料：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.seed_multihop_test
```

跑批在开始前拒绝 stale span，使用模型网关调用当前 query embedding，按版本和字符区间映射
gold span 到 heading chunk。结果写入 `eval_runs/eval_results`，并在 Git 忽略的
`eval/outputs/dense-baseline/` 生成逐样本 JSON 与 Markdown 摘要。报告包括 span Recall@K、固定
token budget Recall、nDCG、α-nDCG、MRR、context precision、拒答 AUROC、当前/最优阈值和延迟。
评测强制记录 embedding model/revision 与 `fallback_enabled=false`；只有 `origin=human` 的真实
人工集可以作为正式质量结论。

## 质量检查

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy app
uv run pytest
```

测试数据库由 Compose 首次初始化为 `workpilot_test`；测试夹具会拒绝连接名称不以
`_test` 结尾的数据库，防止误清理开发数据。
