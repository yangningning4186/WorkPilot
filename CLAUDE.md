# CLAUDE.md — WorkPilot 开发约定

本文件是给 coding agent（Claude Code / Cursor）读的项目上下文与规范。人类同样适用。

---

## 项目一句话

**OpenWorker 的手 + DeepTutor 的眼睛**：一个跑在本机、能读懂你的文档并据此动手的
桌面 Agent。沉浸阅读（locator 寻址 + 引文校验 + 阅读器联动）与本机工具循环（目录授权 /
逐次审批 / 幂等 / Scheduler / Skills / MCP）是同一个 run 的两档工作模式，
配套一套能量化效果的**评测体系**与**三档模型成本治理**。

**单用户产品**，无权限系统与多租户——这是刻意简化，扩展点见 `docs/02-架构设计.md §7`。

**作者自己每天在用。** 所有 badcase 来自真实使用，所有 gold answer 由作者亲手标注。

---

## 目录结构

```
workpilot/
├── backend/            FastAPI 服务
│   ├── packages/       三层架构的下层包（独立 pyproject，见 ADR-0011）
│   │   ├── workpilot-ai/        ★「只懂模型」：网关、路由、缓存、Provider 适配
│   │   └── workpilot-telemetry/ ★「只懂度量」：调用 schema、成本口径、费用闸门契约
│   ├── app/
│   │   ├── rag/        知识库产品：kb/（目录即 KB，FAISS+BM25）/ 编辑器授权
│   │   │                （问答流水线与记忆已退役或并入 cowork）
│   │   ├── cowork/     Cowork 产品：工作台 / 文件 / 格式 Skill + Shell / 连接器 / MCP
│   │   │                reading/  ★ 沉浸阅读：locator 寻址、三层匹配、引文校验
│   │   ├── agent_core/ 框架层：循环、状态、事件契约、压缩、预算、幂等身份
│   │   ├── runstore/   存储层：run / 事件 / checkpoint / 幂等租约 / 会话
│   │   ├── cowork_store/  本机 SQLite + JSONL 适配器（唯一后端）
│   │   ├── telemetry/  上面那个包的 SQLite 适配器
│   │   ├── platform/   鉴权会话、请求身份、限流
│   │   ├── ingest/     文档解析（两个产品共用）
│   │   ├── knowledge_contracts.py  RAG ↔ Cowork 的证据与检索契约
│   │   ├── docedit.py  两个产品共用的文档编辑原语
│   │   ├── llm_bootstrap.py  Settings → ModelGateway 的组装根
│   │   ├── core/       配置、日志、进程内队列 / 唤醒总线、0600 JSON 载体、trace
│   │   ├── schemas/    Pydantic 契约
│   │   └── api/ worker/ cli/   入口适配层
│   └── tests/
├── frontend/           Next.js 应用
├── config/             routing.yaml 等运行时配置（改配置不改代码）
├── eval/               评测框架、数据集、跑批脚本
├── deploy/             docker-compose、Dockerfile、初始化 SQL
├── data/               本地语料（不入 git）
└── docs/               设计文档、ADR、实验台账
```

---

## 环境与命令

**没有容器**（`deploy/docker-compose.yml` 现在是 `services: {}`）。Python 用 uv 锁 **3.12**（3.14 上 ML 依赖无 wheel）。

```bash
# 不需要任何外部服务：PostgreSQL 与 Redis 都已退役，状态全在 ~/.workpilot 下的
# SQLite / JSONL / JSON 文件里，队列和事件唤醒都在进程内（deploy/docker-compose.yml 现在是空的）

# 后端
# 依赖里是 fastapi 而不是 fastapi[standard]，没有 `fastapi dev` 这个 CLI
cd backend && uv sync && uv run uvicorn app.main:app --reload

# 前端
cd frontend && npm install && npm run dev

# 评测（评测模式强制关闭 fallback，否则实验不可复现）
# ⚠️ 检索轨当前是断的（docs/06 §4.5）：四层表随 Postgres 退役，span_recall 那组没有实现。
#    没有 eval.run / eval.calibrate 这两个入口——它们从未存在过。
# Cowork 任务集（2026-08-22 修复）：跑批把控制面指到 <package>/store/，不碰 ~/.workpilot
PYTHONPATH=backend backend/.venv/bin/python -m eval.cowork_runner --label <label> \
  --allow-model-send --authorization-note "<为什么可以把这批 prompt 发给模型>"
uv run python -m eval.compare <baseline-run> <candidate-run> --output-dir <dir>
PYTHONPATH=backend backend/.venv/bin/python -m eval.gate check <report-dir> --against main

# 质量（= PR 门禁的全部内容，见 .github/workflows/ci.yml）
uv run ruff check app tests ../eval packages --config pyproject.toml
uv run mypy app packages/*/src/workpilot_*
uv run lint-imports          # 层次契约（ADR-0011），比 pytest 快两个量级
uv run pytest
cd ../frontend && npm run lint && npm run typecheck
```

**门禁只有两层**：PR 层是静态检查 + pytest（GitHub Actions），夜间层是真语料 + Judge
（跑在本机/集群）。PR 层**不跑评测** —— 理由与代价见 [06 §4.1](docs/06-评测体系.md)。
所以 badcase 回流除了进 `regression` 集，**必须同时固化成 `backend/tests/` 的用例**，
那才是每个 PR 都在挡的那一层。

---

## 十条不可违背的约束

违反这些会导致后期返工，改不动。
**0. 依赖方向单向**：`api/worker/cli → {rag, cowork} → runstore → agent_core → workpilot_ai`，
且 `rag` 与 `cowork` 互不 import。共享只能下沉到 `agent_core` / `runstore` /
`knowledge_contracts.py` / `docedit.py` / `ingest/`。由 `uv run lint-imports` 强制
（[ADR-0011](docs/adr/0011-三层架构与依赖方向.md)）。

1. **所有 LLM 调用必须经过 `workpilot_ai.gateway`**，禁止在业务代码里直接 import
   Provider 实现。构造网关走 `app/llm_bootstrap.py`（唯一允许读 `Settings` 的地方）。
   → 否则路由、缓存、预算、成本统计、trace 全部失效。
   → 这条现在由包边界 + `[tool.importlinter]` 契约 5 强制，不再只靠 code review。

2. **Agent 状态必须是可 JSON 序列化的 TypedDict**，节点是 `state → state` 的纯函数。
   → 这是断点续跑、时间旅行调试、人工中断的地基。禁止把连接、客户端、闭包塞进 state。

3. **任何检索结果必须携带完整溯源元数据**：`block_id` / `doc_id` /
   `locations[]`（每项含 `page_no` / `bbox_norm` / `page_width` / `page_height` / `rotation` / `coord_origin`）。
   → 引用是产品的核心承诺；只存 bbox 四个数换个渲染器就会高亮错位。

4. **面向模型的错误信息**：工具与校验失败返回的 message 是写给 LLM 看的可执行指令，
   不是给人看的 stack trace。
   ✅ `缺少参数 start_date，需要 YYYY-MM-DD 格式，例如 2026-08-01`
   ❌ `ValidationError: field required (type=value_error.missing)`

5. **每个 Agent run 必须有预算上限**（token / 调用次数 / 墙钟时间），任一超限即熔断。
   → 防止反思循环烧钱。

6. **新增或修改任何影响输出的逻辑，必须同步补评测样本**。
   检索策略、prompt、工具描述、模型档位——改了就要有对应的评测样本能验证它。

7. **禁止在 `data/` 之外落语料，禁止把语料提交进 git**。

8. **评测 gold 标注绝不锚 `chunk_id`**。chunk 随分块策略与重新分块而变，标注绑上去就废了
   （[ADR-0006](docs/adr/0006-分块与标注分层.md)）。
   → 原锚点 `parsed_blocks` 的字符区间随 Postgres 退役，**重建时锚
   `(文件 content_hash, 页码, 字符区间)`**。阅读路径的引用溯源仍锚 `ParsedBlock`。

9. **有副作用的工具必须走 `tool_invocations` 幂等协议**，
   不得依赖 Agent 状态里的 `cursor` 或任何步骤序号做去重。
   → interrupt 或崩溃恢复可能重新进入尚未确认完成的执行片段，
   状态恢复 ≠ 副作用不重放（[ADR-0007](docs/adr/0007-agent幂等与事件溯源.md)）。
   重试必须区分有效租约与过期租约；跨系统副作用只承诺 effectively-once，
   并尽量向下游透传同一幂等键。

10. **换了 embedding 就不许拿旧索引检索；候选成功前不许影响 active。** 文件系统上的
    `KbIndexVersion` 固化 embedding 签名与检索配置，每次加载时比对，不一致就拒绝；候选先写
    独立的 `versions/<id>/`，完整成功后才原子发布到 manifest。active 指针失效时拒绝检索，
    不猜测回落（[ADR-0014](docs/adr/0014-知识库索引版本化与显式激活.md)）。
    → 不拒绝的话，旧向量和新查询向量不在同一个空间里，检索不会报错，只会安静地返回
    胡说八道的结果。**无声失败和显式失败的区别，是这条约束的全部理由。**

---

## 代码风格

- **注释用中文**，术语（embedding / rerank / chunk / RRF）保留英文
- Python：全量类型标注，Pydantic 定义所有对外契约，函数超过 50 行就拆
- TypeScript：`strict: true`，不允许 `any`
- 命名：模块名用小写下划线；对外 API 字段用 snake_case（与后端一致，前端不做转换）
- 日志：结构化（`structlog`），每条日志必须带 `trace_id`

## 提交规范

`<type>(<scope>): <中文描述>` — type ∈ feat/fix/refactor/docs/test/chore/exp

`exp` 专用于实验类改动，必须在 `docs/experiments/` 留下对应台账。

---

## 与 AI 协作的分工

**交给 agent 做**：前端组件与页面、文档解析流水线、Docker 与部署配置、数据入库脚本、
评测跑批的工程骨架、看板可视化、测试用例生成。

**必须自己想清楚再让 agent 落地**：检索融合策略、Agent 图结构、记忆的冲突消解规则、
评测指标定义与门禁阈值、模型路由分档、**阅读接地的引文校验强度**（哪些情况拒绝、
哪些情况降级成只翻页不高亮）。
→ 这四块是面试主战场，代码可以是 AI 写的，**设计决策必须是自己的**。

**每次 agent 产出后自查的三件事**：边界情况（断流、超长输入、并发、错误态）、
是否违反上面九条约束、是否偷偷绕过了模型网关。

---

## 常见陷阱

- **恢复状态不等于恢复副作用**：尚未确认完成的执行片段可能重放，工具必须独立幂等
- **SQLite 里时间是字符串，比较是字典序**：全程存 UTC ISO。混了本地偏移，
  `22:59+08:00` 会排在 `15:05+00:00` 后面——两者其实是同一刻，租约与过期清扫会判错
- **SQLite 没有 decimal**：钱一律存整数微美元。用 REAL 存美元会让
  `0.1 + 0.2 != 0.3` 直接发生在预算比较上，而且不报错
- **`os.replace` 是原子的但不排他**：两个进程都会"成功"。要互斥用
  `O_CREAT|O_EXCL` 建锁文件，过期判定读锁里的 claimed_at，不要读 mtime
- **worker 不依附 HTTP 连接**：任务入进程内队列独立执行，客户端只是订阅 `run_events`；
  持久化真相是 SQLite 里的 queued 状态，队列只负责降低唤醒延迟
- **阅读的 locator 里空页也要占一格**：只有图没有文字层的论文页很常见，跳过它会让此后
  所有页码整体偏移一位——模型引第 12 页、用户翻到第 13 页，而且没有任何迹象表明出了错
- **PDF bbox 只有四个数不够**：必须同时存页面尺寸、旋转、坐标原点、归一化坐标；跨页/多区域位置存子表
- **字符偏移统一口径**：NFC + Unicode code point；前端 UTF-16 offset 由后端转换并用 quote 校验
- 中文/学术 PDF 解析后务必抽样人工检查，表格错位与双栏乱序是最隐蔽的质量杀手
- **解析必须跑在子进程并设资源上限**，MinerU 遇畸形 PDF OOM 会拖垮整个服务
