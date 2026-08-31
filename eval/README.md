# eval

自建评测框架。设计见 [docs/06-评测体系.md](../docs/06-评测体系.md)、[ADR-0003](../docs/adr/0003-自建评测框架.md)。

与 backend 平级的一等模块，不是测试目录的附属。

## 统一评测与回放入口

新的回归评测、基线晋升与回放契约见
[docs/18-评测与回放层.md](../docs/18-评测与回放层.md)。交付门禁以 `eval.regression` 为准；
`eval.compare` 保留统计诊断用途，`eval.gate` 只兼容历史 retrieval/generation 报告，不用于新 baseline。

```bash
# 只读检查 track/suite/policy/baseline/replay 目录
PYTHONPATH=backend backend/.venv/bin/python -m eval.catalog doctor

# 零模型、零工具地验证 Run 事件协议与状态折叠
PYTHONPATH=backend backend/.venv/bin/python -m eval.replay verify \
  eval/replays/run-protocol-v1.json --format markdown

# 从本机权威 run_events 导出已完成 Run（敏感、0600、不覆盖）
PYTHONPATH=backend backend/.venv/bin/python -m eval.run_replay_export \
  --run-id <RUN_UUID> --output eval/outputs/replay/<RUN_UUID>.json \
  --acknowledge-sensitive-output

# 从批准报告生成隐私安全 baseline，再对候选做严格配对门禁
PYTHONPATH=backend backend/.venv/bin/python -m eval.regression snapshot \
  <approved-report.json> --policy eval/policies/cowork.json --output <new-baseline.json>
PYTHONPATH=backend backend/.venv/bin/python -m eval.regression check \
  <candidate-report.json> --baseline <baseline.json> --policy eval/policies/cowork.json
```

`eval.regression` 的稳定退出码是 `0=通过`、`1=可比较但发生回退`、`2=拒绝判定`。
当前 catalog 的 Cowork dev/test、KB retrieval、Grounded Generation 四条 track 均已晋升为
独立 v2 baseline；Run 事件回放和 full-chain cassette 也都是 `ready`。Cowork dev（39 条）
和冻结 test（11 条）始终是两个比较分母，不能混成一份 50 条 baseline。

## Office Artifact 评测集

`artifact-rendering-dev-v1.json` 是零模型、零网络的最终文件评测集，当前包含 10 条 PPTX、
DOCX、XLSX、PDF、HTML 正负样本。它覆盖中文粗体字形、SVG/图片、原生图表、文字溢出、
Word 标题与表格、Excel 公式与裁切、PDF 逐页栅格化、离线 HTML 和主动内容拒绝。

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.artifact_suite validate
PYTHONPATH=backend backend/.venv/bin/python -m eval.artifact_suite run \
  --output-dir eval/outputs/artifact-rendering/<label>
```

runner 评分最终保存的 Office 文件，而不是 Spec 或模型文字；输出逐 case 的结构、语义、视觉、
证据、安全状态与 ArtifactBench 指标。输出目录必须不存在，避免覆盖历史结果。当前 suite 为
`synthetic + pending_human_review`，只用于工程回归，不能宣称产品质量基线。

公开集合接入清单位于 `eval/datasets/artifact-benchmarks/catalog.json`。目前登记 PPTC、
PresentBench、DOC2PPT、PPTEval 和 OfficeBench，并显式区分许可证复核、split 冻结与 adapter
状态；没有任何第三方数据被静默下载或伪装成已接入：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.artifact_suite public-catalog
```

### Office 内容评测轨

`office-content-dev-v1.json` 评最终 DOCX、XLSX、PPTX、PDF 的任务内容，而不是只评文件能否
打开或全文关键词。首版含 12 题（8 dev / 4 test）、99 条带来源引用的实例级检查、12 条惩罚
和 36 条带分档锚点的人工/VLM 复核标准。评分先过格式/安全门禁，再分别计算 fundamentals、
completeness、correctness、fidelity、usability；复核未完成时总分保持 `null`。PPT 文本只取观众
可见页面，不把 speaker notes 算入内容；关键数字与标签做局部关系绑定，原生图表直接检查系列
数据。自动轨仍是独立硬门禁，但总分权重调整为自动 60% / 复核 40%；任一复核 criterion
低于最低档都会否决通过，不能被自动分掩盖。

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.office_content_suite validate
PYTHONPATH=backend backend/.venv/bin/python -m eval.office_content_suite prepare \
  --workspace-root /tmp/workpilot-office-content-dev
PYTHONPATH=backend backend/.venv/bin/python -m eval.office_content_suite score \
  --submission-root /tmp/workpilot-office-content-dev \
  --output-dir eval/outputs/office-content/<label>
```

对已经生成到 `submission/` 的文件，可用一条命令同时跑确定性规则、逐页渲染、视觉大模型复核
和最终汇总。命令默认读取已配置的 heavy（否则 main）端点；该模型必须支持图片输入，也可用
`--judge-base-url/--judge-model` 显式指定。Office 文件和题目来源资料会发送给模型，因此必须同时提供发送开关
和非空授权说明：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.office_eval \
  --submission-root /tmp/workpilot-office-content-dev \
  --output-dir eval/outputs/office-content/<model-label> \
  --allow-model-send \
  --authorization-note '仅允许本次 synthetic Office dev 文件发送到内部视觉模型'
```

模型响应采用严格 JSON、固定模型身份、有限调用/token/页数预算，并绑定最终文件 SHA-256；文本
模型拒绝图片时直接失败，不会静默退化成“看不到页面也评视觉”。输出额外包含
`model-reviews.json` 与 `model-review-run.json`。当前 VLM rubric 尚未与办公专家盲评完成校准，
所以报告会给出 `final_score` 和 `engineering_pass`，但 `benchmark_eligible=false`；发布门禁仍只接受
合格人工复核，不能把模型工程分冒充正式 benchmark 成绩。

发布候选必须额外传 `--reviews <reviews.json> --require-complete-reviews`；报告会固定 suite SHA-256、
scorer fingerprint 和生成时间。`office-eval-contract` 已登记到 catalog/nightly，零模型回归最终
文件验证、四格式代表 oracle、三类 PPT 防投机反例、复核否决和 Cowork Office 工作流覆盖：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m pytest -q \
  backend/tests/test_artifact_eval_suite.py \
  backend/tests/test_office_content_suite.py \
  backend/tests/test_cowork_task_suite.py
```

test 访问必须额外传 `--include-test --test-access-note '<原因>'`。完整方法、评分公式、复核文件
格式与晋升条件见 [docs/20-办公文件内容评测集.md](../docs/20-办公文件内容评测集.md)。当前仍为
`synthetic + pending_human_review` 候选集，不得冒充正式 baseline。

`agent-teams-dev-v1.json` 仍是待人工复核的 candidate，不能伪装成已晋升 baseline；它以
`agent-teams-contract` 单独登记到 catalog。该 contract 是零模型、零外部 I/O 的 pytest gate，
固定写委派 receipt、scope 越权/篡改拒绝、返工预算和进程重启恢复四类边界。

## Cowork 单 Agent 50 条基线

`cowork-core-50-v1.6.1.json` 是当前 catalog/nightly 已批准的 Cowork 端到端集：39 条 dev、11 条冻结 test，
覆盖 workspace（含只读 git 视图）、artifact、格式 Skill + Shell、Web、knowledge/RAG、工作区文档沉浸阅读
与安全/HITL，并新增飞书连接器和持久 shell 两类回归任务。每条记录均包含
可复现 fixture、初始 capability、期望终态、gold 工具、工具顺序/调用预算和确定性成功断言；
knowledge 类额外强制 `EvidenceBundle` 合约，不允许 `chunk_id`、内部 score 或 ORM 泄漏；
沉浸阅读路径强制 locator 引用（`[p.N]`）。若 prompt 已给出精确 Markdown 路径，完整
`read_file` 加章节/行号引用也算可溯源，不能强迫它伪造页码；负例则要求在给出任何候选
数字前先明确说明文档不可答，避免仅凭对同名论文的印象编答案。

套件保留生成来源 `origin=synthetic`。行之签字批准的 v1.6.0 原文件继续冻结保留；v1.6.1
只修复 034 的模型可见引用断言、040 的写权限和 044 的 baseline/空白确定性断言，并于
`2026-08-25T10:42:21+08:00` 由行之重新复核批准。这不改变冻结 test 不得用于调参的约束。
本地 CLI 默认的 `v1.6.2` 仍是 `pending_human_review` 候选版本；在签字、重跑并晋升独立 baseline
之前，不得把它的结果与 v1.6.1 baseline 混比或称为正式回归结论。

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.cowork_task_suite
PYTHONPATH=backend backend/.venv/bin/pytest backend/tests/test_cowork_task_suite.py -q
```

runner 会为每题创建隔离工作区和独立 Cowork Run，走生产 `run_cowork_graph`；Web 与 RAG
改用 suite 内的确定性 adapter，不访问公网或生产资料库。默认只跑 dev：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.cowork_runner \
  --label cowork-dev-v1 --split dev --allow-synthetic --allow-model-send \
  --authorization-note '<已核验的模型端点与合成数据发送授权>'
```

live runner 默认按整批强制 `2,000,000 token / 400 次模型调用 / 5,000 秒墙钟` 三重熔断。
每条 Run 会拿到整批剩余额度作为更紧的上限，模型 dispatch 前按最坏 token 用量预留，因此并发或
单条长任务不能先穿透再记账；基础设施失败导致用量无法结算时整批 fail closed。`--budget-tokens`
仅为旧命令兼容且只接受 `0`；`--budget-calls`、`--budget-wall-ms` 可额外收紧单条 Run，
`--max-total-tokens`、
`--max-model-calls`、`--max-wall-seconds` 配置整批上限，三项总量上限都必须为正数。

跑完整 50 条必须显式留下 test access 审计：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.cowork_runner \
  --label cowork-core50-v1 --split all --include-test \
  --test-access-note '<本次冻结验收原因>' \
  --allow-synthetic --allow-model-send \
  --authorization-note '<已核验的模型端点与合成数据发送授权>'
```

输出位于 `eval/outputs/cowork-core/<timestamp>-<label>/`：`observations.jsonl` 保留逐题
checkpoint/tool trace，`report.json` 提供机器可读总表及 category/split 分层，`report.md`
提供人工摘要。主指标是规则轨 `task_success_rate`、Gold 工具选择准确率、
`actual_tool_calls / optimal_tool_calls` 步骤效率、P95 延迟和 Token；同时报告工具错误率与恢复数。
runner 逐题落盘，某一题或进程失败不会抹掉已经完成的 observation。

**动过阅读工具的题另算一层**（`metrics/reading.py`，口径见 [docs/04 §5](../docs/04-知识与阅读设计.md)）：
`read_before_claim`（回答里每个 `[p.N]` 之前有没有真的 `read_material` 过那一节）、
`quote_verifiability`（交给 `reader_goto` / `reader_annotate` 的引文能不能逐字回原文，
**按书写体系分桶并单列 cross-language**）、`locator_accuracy`（在已验证存在的引文里，真身所在
的 locator 是不是模型声称的那个）。三条的分母互不重叠，落在 `report.json` 的
`metrics.reading` 与每条样本的 `score.reading`；没碰阅读工具的题整条不计入——"没考"和
"考砸了"必须长得不一样。这一层完全是确定性的，所以离线重评分（下面那条命令）能原样重算。

若只调整了标注或确定性 scorer，可复用既有 observation 离线重评分，不再次调用模型；延迟、
Token 和工具轨迹保持原值，并在 manifest 记录源报告哈希：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.cowork_runner \
  --label cowork-core50-v1-rescored \
  --rescore-report eval/outputs/cowork-core/<baseline>/report.json \
  --include-test --test-access-note '<离线重评分原因>' --allow-synthetic
```

首轮人工复核应逐条确认：prompt 是否自然且无歧义、fixture 是否足以作答、gold 工具是否是
最短安全路径、断言是否真的代表任务完成、HITL/权限预期是否符合产品策略。复核完成后再提升
版本并冻结 test split；不要直接修改已经产生正式报告的版本。

正式快照会强制 suite `approved`、reviewer/带时区 reviewed_at、`git_dirty=false` 和精确 split。
dev 报告只能生成 `eval/snapshots/v2/cowork-core-dev.json`；test 必须另跑并生成
`eval/snapshots/v2/cowork-core-test.json`，不能把 50 条混成一个比较分母。

live Cowork 跑批会自动生成权限 `0600` 的 `model-cassette.json`。在同一 suite/split/item
顺序上可用它进行零真实模型 dispatch 的 fixture 执行回放：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.cowork_runner \
  --suite eval/suites/cowork-core-50-v1.6.1.json --label replay-cowork-dev \
  --split dev --allow-synthetic \
  --replay-cassette eval/outputs/cowork-core/<recorded>/model-cassette.json
```

cassette 含完整 prompt/模型响应，不得提交 Git 或上传公共 artifact。请求漂移、篡改、
cassette miss 或未消费记录都 fail closed；当前无可验证的断网 sandbox，所以 gold 或
cassette 实际响应含 `run_shell` 的 case 会在 graph 执行前被拒绝。

## RAG、工具与外部副作用 full-chain cassette

模型 cassette 只封住模型请求；`full_chain_cassette.py` 进一步在 RAG、Cowork 工具和外部写
副作用三个 I/O 边界做严格顺序录制。每条 interaction 同时固定规范化请求摘要、前序摘要和
自身摘要，顶层完整性再覆盖整份 cassette。重放时传入的真实 delegate 会被完全忽略，因此
不会重新检索生产 KB、执行工具或重复发送外部写操作；请求、顺序、hash chain、未消费记录
任一不一致都会 fail closed。外部写必须携带非空 idempotency key，录制的是返回 receipt。

真实录制默认标记为 `sensitive`、权限 `0600`，只能放在忽略的 `eval/outputs/`；Catalog 只接受
`origin=synthetic` 且 `data_classification=synthetic` 的提交文件。仓库里的三段合成链路可这样
做零 I/O 验证：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.full_chain_cassette \
  eval/replays/full-chain-v1.json --format json
```

## Nightly

`.github/workflows/eval-nightly.yml` 每天北京时间 02:00 在带 `workpilot-eval` 标签的自托管
runner 上运行 Cowork dev、冻结 test、KB evaluation 和 Generation 70 条，再与固定 v2
baseline 配对比较；同一矩阵还执行 `agent-teams-contract` 的四条和
`control-plane-contract` 的 30 条、`office-eval-contract` 的 10 条零真实模型 deterministic case。模型固定为
`deepseek-v4-flash`；nightly 默认总上限为 `6,000,000 token / 1,200 次模型调用 /
18,000 秒墙钟`，并把剩余额度下压给每个 live 子进程。子进程超时会终止整个 process group；
报告缺少或存在未结算 usage 时停止后续 live track 并失败。当前报告没有可靠、完整的模型定价，
因此明确记录 `cost_limit=not_enforced_without_reliable_pricing`，不伪造金额熔断。KB 与索引由自托管 runner
持有；总调用/token 台账覆盖 `ModelGateway`，本地 KB query embedding 因底层客户端不返回 usage，
由冻结 item 数和 track 墙钟上限约束并在 summary 明示该口径。任务不会改写 baseline。原始 report、prompt、模型 cassette 只在本机保留 30 天且至少
保留最近 7 批；上传的 artifact 仅含 Catalog doctor、full-chain 零 I/O 验证、回归指标与摘要，
GitHub 保留期同为 30 天。

Rerank 按 track 固定，而不是由 workflow 的全局开关覆盖：KB retrieval 使用
`RERANK_ENABLED=true`，与冻结校准的 `score_source=rerank` 一致；Generation 使用
`RERANK_ENABLED=false` 保持 fusion baseline。配置要求 reranker 时发生 fallback，仍会让
对应 retrieval 跑批 fail closed。

## Skill paired gate

`cowork_runner` 支持 `--skills-mode enabled|disabled` 和隔离的 `--skills-root`。disabled 臂
不注册 Skill 工具，也不注入 Skill catalog；enabled 臂走生产 catalog 与 `load_skill` 路径。
除 `skills_mode` 外，两臂的模型、suite、Skill root hash、预算、scorer 与实现配置必须一致：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.cowork_runner \
  --suite eval/suites/skill-paired-dev-v1.json --label skill-disabled \
  --skills-mode disabled --skills-root eval/fixtures/skills \
  --allow-synthetic --allow-model-send --authorization-note '<已核验授权>'

PYTHONPATH=backend backend/.venv/bin/python -m eval.cowork_runner \
  --suite eval/suites/skill-paired-dev-v1.json --label skill-enabled \
  --skills-mode enabled --skills-root eval/fixtures/skills \
  --allow-synthetic --allow-model-send --authorization-note '<已核验授权>'

PYTHONPATH=backend backend/.venv/bin/python -m eval.skill_paired_gate \
  --suite eval/suites/skill-paired-dev-v1.json \
  --disabled-report /path/to/disabled/report.json \
  --enabled-report /path/to/enabled/report.json \
  --output-dir eval/outputs/skill-paired-gate/RUN
```

gate 同时检查触发激活、反触发误激活、任务成功、guardrail、工具错误、calls/tokens 和 paired
bootstrap。仓库内套件是 synthetic，只能输出 `engineering_only_no_product_claim`；正式晋升还需
owner 审核的人类题集与盲评。

## Team quality/cost paired baseline

`team_quality_baseline` 按 case 交替两臂顺序。single 臂对两份材料做一次综合；Team 臂使用生产
Team store、两个持久 Worker Session、只读 Board scope、durable wake、Worker 工具循环、Board
review 与 Lead 综合。两臂共享同一个模型网关和 calls/tokens/墙钟熔断：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.team_quality_baseline \
  --suite eval/suites/team-quality-paired-dev-v1.json \
  --label team-quality-RUN --allow-synthetic --allow-model-send \
  --authorization-note '<已核验授权>'
```

报告比较 task success、敏感信息 guardrail、Board 完成率、Worker 失败率、模型 calls、tokens、
墙钟和 paired bootstrap，并生成隐藏臂标识的盲评模板。内置 synthetic suite 在 owner 审核前同样
只能作为工程基线。

## A5 长期记忆注入

`memory_injection_experiment.py` 对同一批个人化任务严格配对运行 memory off/on，冻结模型、
温度和 token 上限，样本顺序交替；报告 task success、paired bootstrap 区间、输入 token 与延迟。
同时生成打乱臂标识的 `blind-review.jsonl`，满意度只能由 owner 填写。

仓库中的 `a5-memory-seed.json` 明确标为 synthetic，只能验证工程链路，不能作为产品质量结论：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.memory_injection_experiment \
  --suite eval/suites/a5-memory-seed.json --label a5-seed-YYYYMMDD \
  --allow-model-send --authorization-note '<已核验的端点与授权记录>' --allow-synthetic
```

runner 使用 evaluation mode，禁止 fallback。即便是合成 seed，也必须显式确认模型端点与发送授权；
真实 owner 记忆更不能在端点信任边界不清楚时外发。正式 A5 结论要求 owner 审核的 human suite、
完整 paired report 和盲评满意度，三者缺一不可。

模型跑完后，owner 在 `blind-review.jsonl` 逐条填写 `preferred=A|B|tie`、两臂 1–5 分、
`reviewer` 与 `reviewed_at`，再解盲汇总：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.memory_blind_review \
  --package eval/outputs/memory-injection/<label>
```

缺评分、缺 reviewer/时间、item 顺序漂移或不足 5 条都会 fail-closed，不生成满意度报告。

A5 owner 盲评后发现的 `a5-003/004/010` 不回写到已冻结的 seed，单独放在
`eval/suites/a5-memory-quality-regression.json`。该集同时要求：记忆不得压缩通用答案的
关键信息，回答不得暴露 `[M1]`、`user_context`、`personal_memory`、
个人记忆/背景的内部来源或“根据记忆”等表述。
它是事后回归集，不得替代原始 A5 作为独立增益证据。

独立的下一轮使用预注册 A6 suite 与匿名配对语义 Judge，不再以关键词覆盖率作为主结论：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.memory_semantic_experiment generate \
  --suite eval/suites/a6-memory-semantic-preregistered.json --label <label> \
  --allow-synthetic --allow-model-send --authorization-note '<generation approval>'

PYTHONPATH=backend backend/.venv/bin/python -m eval.memory_semantic_experiment judge \
  --package eval/outputs/memory-semantic/<label> \
  --provider openai_compatible --model <independent-judge-model> --base-url <approved-v1-url> \
  --allow-model-send --authorization-note '<judge approval>'
```

生成和 Judge 必须分别授权。Judge 看不到 `memory_on/off` 臂名；正式结论默认拒绝生成模型
自评。任务完整性、记忆使用与来源泄漏分开计分，确定性泄漏轨仍是硬失败。完整预注册门槛
见 `docs/experiments/2026-08-18-A6-长期记忆语义评测预注册.md`。

## 当前文件系统 KB 检索基线

`kb_retrieval_runner.py` 直接走生产中的 `LocalKbService -> search_index`，不恢复已经退役的
PostgreSQL 表。gold 锚点固定为 `(content_hash, page_no, char_start, char_end)`：PDF 字符区间
相对物理页起算，Markdown/TXT 的 `page_no` 为 `null`。runner 会在第一条 query 发出前把所有
quote 逐字映射回选定索引；内容、页码或区间漂移时整批拒绝运行。

先用索引中的逐字原文生成可复制的 anchor：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.kb_retrieval_runner anchors \
  --kb-slug <slug> --kb-version <version> --quote '<原文中的连续引文>'
```

评测集是独立 JSON 文件，最小结构如下。可答题必须有事实组，不可答题必须没有伪造证据；
同一事实的多个等价出处放进同一个 `alternatives`，多跳问题则放多个事实组。

```json
{
  "schema_version": 1,
  "name": "my-kb-dev-v1",
  "origin": "synthetic",
  "review": {"status": "pending_human_review"},
  "items": [{
    "item_id": "dev-001",
    "split": "dev",
    "category": "single_hop",
    "question": "……？",
    "answerable": true,
    "gold_evidence_groups": [{
      "fact_id": "R1",
      "alternatives": [{
        "content_hash": "<64 位 sha256>",
        "page_no": 3,
        "char_start": 149,
        "char_end": 176,
        "quote": "……"
      }]
    }]
  }]
}
```

跑 dev 候选集：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.kb_retrieval_runner run \
  --suite /path/to/kb-dev.json --kb-slug <slug> --kb-version <version> \
  --label current-hybrid-v1 --top-k 10 --diagnostic-k 50 \
  --token-budget 4000 --allow-synthetic
```

不带阈值的报告可以用于工程诊断，但不能晋升正式 retrieval baseline。仓库中的独立候选集
`eval/suites/kb-rag-research-refusal-calibration-v1.json` 有 8 条可答、4 条不可答，其证据文档
与 26 条 evaluation gold 无交集，并已由行之批准。正式流程先以相同 KB/index、预算和真实
score source 跑这份 calibration suite，再冻结阈值：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.refusal_calibration \
  --report eval/outputs/kb-retrieval/<calibration-run>/report.json \
  --reviewer '<复核人>' --reviewed-at '<带时区 ISO-8601>' \
  --output eval/calibrations/<new-calibration>.json

PYTHONPATH=backend backend/.venv/bin/python -m eval.kb_retrieval_runner run \
  --suite /path/to/kb-evaluation.json --kb-slug <slug> --kb-version <version> \
  --label current-hybrid-v1 --top-k 10 --diagnostic-k 50 --token-budget 4000 \
  --refusal-calibration eval/calibrations/<new-calibration>.json
```

runner 从逐题命中读取实际 `retrieval_score_source`，不再相信配置推断；同一跑批混用量纲、
reranker 配置开启却 fallback、校准/evaluation suite SHA 相同，都会在写报告前拒绝。

输出位于 `eval/outputs/kb-retrieval/<timestamp>-<label>/`，包含 `report.json` 和 `report.md`。
报告记录 suite/config/index/实现指纹、逐题命中与失败归因、Recall/nDCG/α-nDCG/MRR、上下文
精度、文档覆盖、token、延迟、可答/不可答分数分布和拒答 AUROC。它保持现有
`eval.compare` / `eval.gate` 的报告形状；只有 owner 逐题复核并把 `origin` 升级为 `human`
之后，才允许导出新的正式 baseline 快照。冻结 test split 必须额外携带
`--include-test --test-access-note '<原因>'`。

`--top-k` 是产品正式召回深度；runner 会以这个深度单独调用一次 `search_index`
并据此计分。只有可答题漏召回时，才用 `--diagnostic-k` 再跑一次不精排的深层检索；
这份结果只用来区分 `outside_top_k` / `document_not_retrieved` 等归因，**不会再切回 Top-K
混入正式指标**。

当前可复跑的多文档候选集是 `eval/suites/kb-rag-research-dev-v1.json`：53 篇论文上的
22 条可答题与 4 条不可答题；它保留 `origin=synthetic`，并已由行之人工批准。E2-FS 固定
其余配置，只比较已保留的 `e2-dense` 与 active `v1 hybrid`：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.kb_retrieval_runner run \
  --suite eval/suites/kb-rag-research-dev-v1.json \
  --kb-slug rag-research --kb-version e2-dense --label e2-dense-control-v1 \
  --top-k 10 --diagnostic-k 50 --token-budget 4000 --allow-synthetic

PYTHONPATH=backend backend/.venv/bin/python -m eval.kb_retrieval_runner run \
  --suite eval/suites/kb-rag-research-dev-v1.json \
  --kb-slug rag-research --kb-version v1 --label e2-hybrid-candidate-v1 \
  --top-k 10 --diagnostic-k 50 --token-budget 4000 --allow-synthetic

PYTHONPATH=backend backend/.venv/bin/python -m eval.compare \
  <dense-report-dir> <hybrid-report-dir> \
  --output-dir eval/outputs/kb-retrieval-compare/<label> \
  --primary-metric ndcg_at_k --experiment-variable retrieval.engine
```

完整约束、指标和结论见
[`E2-FS`](../docs/experiments/2026-08-23-E2-FS-文件系统KB混合检索单变量对照.md)。

E3-FS 在同一个 active hybrid 索引上只切换 `rerank.enabled`。先启动
[`reranker/`](../reranker/README.md) 的本机服务，再跑 Top-5 对照：

```bash
PYTHONPATH=backend RERANK_ENABLED=false backend/.venv/bin/python -m eval.kb_retrieval_runner run \
  --suite eval/suites/kb-rag-research-dev-v1.json \
  --kb-slug rag-research --kb-version v1 --label e3-top5-rrf-control-v3 \
  --top-k 5 --diagnostic-k 50 --token-budget 4000 --allow-synthetic

PYTHONPATH=backend RERANK_ENABLED=true RERANK_CANDIDATE_K=10 \
RERANK_CANDIDATE_TEXT_MODE=content \
backend/.venv/bin/python -m eval.kb_retrieval_runner run \
  --suite eval/suites/kb-rag-research-dev-v1.json \
  --kb-slug rag-research --kb-version v1 --label e3-top5-rerank10-candidate-v3 \
  --top-k 5 --diagnostic-k 50 --token-budget 4000 --allow-synthetic

PYTHONPATH=backend backend/.venv/bin/python -m eval.compare \
  <rrf-report-dir> <rerank-report-dir> \
  --output-dir eval/outputs/kb-retrieval-compare/<label> \
  --primary-metric ndcg_at_k --experiment-variable rerank.enabled
```

完整结果和选参过程见
[`E3-FS`](../docs/experiments/2026-08-23-E3-FS-本地cross-encoder精排.md)。

## PostgreSQL 时代的 Dense-only 基线（历史）

> 下方 `dense_baseline` / `suite_retrieval_runner` 是 PostgreSQL 时代的历史复现实验；
> ADR-0012 退役数据库后不再是当前 KB 的运行入口。当前文件系统 KB 请使用上一节的
> `kb_retrieval_runner`，不要把旧快照当成当前质量基线。

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

# 只跑词法单臂。RRF 里 dense 会兜住词法的失效, 只看融合结果无法归因词法打分的好坏
--strategy lexical-only
```

词法打分方式是独立的单变量开关，进 `config_hash`：

```bash
# ts_rank(默认): 分语言 tsvector, english 配置负责停用词与词干, 中文走 bigram
# coverage:      命中词数 / 总词数, 无停用词表、无 IDF、子串匹配(旧默认)
# ts_rank_cd:    同上但用 cover density —— 对中文 bigram 有害, 保留为反例, 见台账 E2
--lexical-mode ts_rank
```

## 数据集

| 名称 | origin | 条数 | 用途 |
|---|---|---:|---|
| `core-dev` | human | 20 | 作者亲手标注的中文与跨语言问题 |
| `core-dev` | synthetic | 6 | hard-negative 候选，不计入 M0 正式 40 条 |
| `multihop-test-v1` | synthetic | 8 | PDF multi-hop 留出集 |
| `english-dev` | human | 20 | AI 构造候选后由 owner 逐条复核升级；补全英文检索盲区（台账 E3） |
| `dense-title-smoke` | synthetic | 15 | 只验工程链路，不作质量结论 |

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.seed_english_dev
```

`english-dev` 的两条有效性前提由种子脚本强制，违反即拒绝入库：问题不含任何 CJK 字符；
问题不与 gold 原文连续重合 4 个词（抄原文会让词法臂靠字面重合命中，测的就不是检索能力）。
种子脚本仍只写入 `synthetic` 候选；不能用脚本自动冒充人工标注。当前 human 身份来自 owner
逐条复核与明确确认，provenance 固化在 `eval/suites/m0-core-40.json`。

### M1 80 条 human 基线（70 dev + 10 test）

曾规划的 120 条候选构建器仍保留用于复现失败实验：它保留 40 条 human 原集，另生成
60 dev + 20 test 自动草稿；首版草稿被内容门禁全部拒绝，没有写库。当前正式基线不再硬凑
120 条，而是原 40 条加 40 条人工撰写、作者逐条复核的 human 数据，共 70 dev + 10 test。
test 按 document version 隔离，最终方案冻结前不得访问。

```bash
# 只生成 ignored 的 manifest、review-dev.jsonl、review-test.jsonl 与报告
PYTHONPATH=backend backend/.venv/bin/python -m eval.build_m1_candidate_suite

# 连接 DB 独立复核 Unicode quote/range、parsed block、配额、schema 与泄漏
PYTHONPATH=backend backend/.venv/bin/python -m eval.audit_m1_candidate_outputs \
  eval/outputs/dataset-candidates/<fingerprint-prefix>

# 只有内容质量门禁通过后才允许写四个隔离 staging dataset
PYTHONPATH=backend backend/.venv/bin/python -m eval.build_m1_candidate_suite --apply
```

候选构建器和审计器均 fail-closed：通用模板、`block N` 占位、整块 quote 直接冒充 gold answer、
跨 split question/span/version 重复、与既有 40 条问题重复、缺完整 review schema、非
`synthetic/pending_human` 或存量同 ID 内容漂移都会拒绝。被拒绝的草稿不计入正式基线，
也不会创建 staging dataset；人工逐条重写并填写 reviewer/reviewed_at 前不得升级为 human。

PostgreSQL 退役后，70 条 dev 的正式生成套件迁到
`eval/suites/m1-dev-70-v2.json`。它内嵌全部问题、答案、约束和事实组，gold 使用
`content_hash + page_no + char range`，不再依赖本机 `eval_items` UUID。37 篇冻结 corpus
来自历史 70 条 retrieval 报告的 Top-10 `source_uri` 并集；迁移器保留输入报告 SHA-256，
规范化精确匹配失败时才做有下限的模糊重定位。

```bash
# 一次性准备独立 KB。来源 v1 的 embedding/chunk/RRF 签名必须完全一致；
# dense 节点与向量无损组合，BM25 对 37 篇并集重新计算，不修改三个来源 KB。
PYTHONPATH=backend backend/.venv/bin/python -m eval.generation_runner prepare-kb

# 正式跑批默认强制 clean Git；会发送问题与截断证据，必须携带授权说明。
# RERANK_ENABLED=false 是明确实验配置，不是 reranker 故障后的静默 fallback。
RERANK_ENABLED=false PYTHONPATH=backend backend/.venv/bin/python \
  -m eval.generation_runner run --label m1-dev70-generation-v2 \
  --allow-model-send --authorization-note '<approval reference>'
```

runner 直接走生产 `search_index`，再由 `ModelGateway` 生成；默认整批上限为
`1,500,000 token / 150 次模型调用 / 5,000 秒墙钟`。因为该 task type 允许 provider
省略单次输出上限，每次 dispatch 前按所选模型完整 context window 做并发预留；失败或缺 usage
按预留额结算。`evaluation_generation` 对支持
省略输出上限的 provider 不下发 provider `max_tokens`，报告中的 `token_budget=null` 只表示
该 provider 字段省略，不表示整批 token 熔断关闭。正式指标为
`citation_wellformed_non_refusal`、`citation_support_answerable` 和
`constraint_pass_answerable`；可答题拒答或零引用都计 0，不能靠缩分母刷分。声明启用 rerank
但实际 score source 发生 fallback 时，该题记为基础设施错误，不进入可晋升 baseline。

对 answerable 误拒做 evidence gate 分层归因时，使用同一份正式 retrieval report 和各 dataset 的
generation report 重建证据。`round_robin` 用于复现旧实现，修复后生产口径为 `sequential`；脚本
会把 Top-K 检索缺失、gate 打包缺失、12k answer 证据预算缺失和 gate 模型误判分开：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.evidence_gate_analysis \
  --retrieval-report /path/to/retrieval-report.json \
  --generation-report /path/to/core-report.json \
  --generation-report /path/to/english-report.json \
  --packing-mode sequential --expected-false-refusals 13 \
  --output-dir eval/outputs/evidence-gate-analysis/<batch>
```

## M0 40 条正式套件与生成规则轨

`m0-core-40` 固定组合 `core-dev` 20 条和 `english-dev` 20 条，不复制底层 gold；运行前会校验
数据集条数、14/8/10/8 类别分布、origin、stale span 与可答题 gold 完整性。

```bash
# 生成 + 拒答 + citation_validity + constraint_pass；M0 固定 dense-only
PYTHONPATH=backend backend/.venv/bin/python -m eval.generation_baseline \
  --dataset core-dev --origin human --label M0-formal-generation-core --top-k 5

# 两个 dataset 各跑一次后，合并检索与生成 report.json
PYTHONPATH=backend backend/.venv/bin/python -m eval.m0_report \
  --retrieval-report /path/to/core-retrieval.json \
  --retrieval-report /path/to/english-retrieval.json \
  --generation-report /path/to/core-generation.json \
  --generation-report /path/to/english-generation.json \
  --output-dir eval/outputs/m0-baseline/<run>
```

`citation_validity` 只校验引用格式、记录映射、block/version/document、字符区间和 quote 原文；
语义 `citation_accuracy` 必须填写 `citation-review.csv` 的 `supported`、`reason`、`reviewer`、
`reviewed_at` 后重新合并报告，未覆盖全部实际引用时保持 `pending_human_review`。

完整复核后先做只读校验，再显式写回 `eval_results.human_label` 和 `eval_runs.metrics`：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.import_citation_review \
  --generation-report /path/to/core-generation.json \
  --generation-report /path/to/english-generation.json \
  --review-csv /path/to/citation-review.csv

# 上一步通过后追加 --apply
```

**动词法检索、分词或查询改写的实验，必须同时报中文集与英文集**——
E2 的教训是全中文题集会把英文失效整个藏起来。

远端策略运行前必须确认数据外发范围与目标端点。正式单变量对照应保持 dataset、Top-K、
diagnostic-K、token budget、embedding identity 和 gold span 不变。

带 rerank 的策略需要先启动本机 cross-encoder 服务（见 [reranker/README.md](../reranker/README.md)）；
服务不可用时跑批直接失败，不会静默退回原顺序，避免把降级结果记成实验数字。

送给 cross-encoder 的候选文本可作为单变量切换，用于复现 D6 的三档对照：

```bash
--rerank-candidate-text-mode title_heading_content   # 默认, D6 判定最优
--rerank-candidate-text-mode heading_content
--rerank-candidate-text-mode content
```

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
M0 报告本身仍不调用 Judge；校准工程入口如下。

## Judge 校准

`judge_calibration.py` 将数据准备、人工标注、模型跑批、版本化写回和一致性门禁拆开。
除 `run` 外均为离线操作；`run` 默认拒绝发送，必须显式声明 provider/model、目标端点和授权说明。
当前 heavy 档尚未接入统一路由，因此不得把 main 档身份冒充 heavy，也不允许 fallback。
已准备独立端点检查器，只验证 exact model identity；`--chat-smoke` 使用不含项目数据的固定合成
提示。健康检查通过不构成 Judge 数据发送授权：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.heavy_endpoint_check \
  --chat-smoke --output eval/outputs/judge-calibration/<batch>/heavy-endpoint.json
```

先从一份包含**唯一题目**的 generation report 导出校准包：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.judge_calibration prepare \
  --generation-report /path/to/unique-cases/report.json \
  --output-dir eval/outputs/judge-calibration/<batch>
```

同一 `dataset/item_id` 出现在多个策略或 run 会直接失败，不能用多策略结果把同一题重复凑数。
当前中间基线要求至少 70 个唯一 case，并覆盖
`single_hop/multi_hop/table/temporal/unanswerable/global` 六类；按 category 固定拆分
calibration/validation，validation 至少 17 条。`agent_task` 在执行闭环实现前不纳入本轮，
因此本轮只允许声明“六类中间校准”。语言不是 category，现阶段按 dataset 切片；若要在同一
dataset 内再分语言，需先把 language 元数据固化进 generation report。

`prepare` 同时生成按 calibration 优先排序的 `human-labels.csv` 和 `human-review-guide.md`。
按当前冻结 binary v2 填完 0/1、理由、reviewer、reviewed_at 后才可跑 Judge。以下命令只是模板，
没有针对数据与目标端点的新授权时不要执行：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.judge_calibration run \
  --examples eval/outputs/judge-calibration/<batch>/examples.jsonl \
  --output eval/outputs/judge-calibration/<batch>/judge-predictions.jsonl \
  --provider openai_compatible --model <heavy-model> --base-url <approved-v1-url> \
  --allow-model-send --authorization-note '<approval reference>'
```

每条 Judge 输出必须先给理由再给 0/1，记录 raw output、rubric/prompt 指纹、实际 provider/model、
token audit 和授权说明指纹；实际身份不同立即失败。标签与 Judge 输出可先只读校验，再显式写回：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.judge_calibration import \
  --examples /path/to/examples.jsonl --human-labels /path/to/human-labels.csv \
  --judge-predictions /path/to/judge-predictions.jsonl --output /path/to/db-patch.jsonl
# 校验通过后追加 --apply；写入版本化 judge_calibration/rubric/metric namespace，
# 不覆盖 M0 human_label.citation_accuracy。
```

最后在未参与 rubric 调整的 validation split 上执行门禁：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.judge_calibration calibrate \
  --examples /path/to/examples.jsonl --human-labels /path/to/human-labels.csv \
  --judge-predictions /path/to/judge-predictions.jsonl --output-dir /path/to/report
```

报告包含固定 `[0,1,2]` 轴的 quadratic weighted Kappa、准确率、混淆矩阵与边际、类别/可答性/
dataset 切片、配对 bootstrap CI 和逐条分歧理由。当前默认门槛为 70 个唯一 case、validation
至少 17 条、整体 QWK/准确率 ≥0.85；类别切片因样本量小只作诊断，不进 gate。缺失/重复标签、
内容或 rubric/prompt 漂移、常量标签导致 QWK 未定义、bootstrap 不完整都会 fail-closed。
不同 metric 或 rubric 版本必须使用独立 namespace，不得把 correctness、faithfulness 与
citation 分数混合统计。

bootstrap 对 validation 做成对有放回抽样。小样本偶尔会抽到单一标签，此时 QWK 的
chance-agreement 分母为 0、数学上不可定义；runner 会丢弃整对退化样本并按固定 seed 补抽，
直到 accuracy/QWK 同时取得请求数量。报告必须记录 `attempted_resamples` 和
`discarded_undefined_qwk`；原始标签本身常量或 10 倍尝试仍凑不齐时继续 fail-closed。

## 两次跑批的配对对照

`compare.py` 只读两份 `report.json`，不连数据库、不调模型、不重跑检索；
`stats.py` 提供逐样本 paired bootstrap（[docs/06 §4.3](../docs/06-评测体系.md)）：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.compare \
  eval/outputs/dense-baseline/<baseline-run> \
  eval/outputs/dense-baseline/<candidate-run> \
  --output-dir eval/outputs/compare/<label>
```

位置参数可以是 `report.json` 本身，也可以是它所在的跑批目录。支持 `dense_baseline`
（检索轨）与 `generation_baseline`（生成轨）两类报告；`refusal_baseline` 报告没有
`item_id`，无法配对，跑批会直接拒绝。

配对与兼容性校验在计算任何指标之前执行，任一条不满足即拒绝出报告：

- 两份报告必须是同一 `dataset`、同一类型，且 `item_id` 集合完全相同；
- 逐条比对 `category` 与 `answerable`，不一致说明标注已漂移，配对无效；
- 受控配置项（检索轨 `origin` / `top_k` / `token_budget` / `theta` / `alpha`）不同则
  两侧算的不是同一个指标，默认拒绝，确属有意对照时用 `--allow-config-drift` 放行；
- 其余配置差异全部列进报告的「配置差异」表——那才是被对照的实验变量。

统计口径：配对百分位 bootstrap，默认 `--resamples 10000`、`--seed 12345`、`--ci-level 0.95`，
同一份输入永远给出同一个区间。所有指标共用同一批重采样下标，因此指标之间的区间可以并排解读；
类别切片在切片内部单独重采样。**置信区间跨 0 就是"无显著差异"，不允许写成提升**。

逐样本表按主指标（检索轨默认 `budget_span_recall`，生成轨默认 `constraint_pass`，
可用 `--primary-metric` 覆盖）给出变好 / 变差 / 持平 / 不适用四类，`--top-n` 控制
markdown 里各列几条，`report.json` 始终保留全部逐样本差值。

一个样本只要在任一侧不适用某指标（不可答题没有检索指标、一侧拒答另一侧作答、
一侧跑批报错），两侧就一起剔除，剔除数量记在「仅一侧适用」列——
否则比较的是两批不同的样本。因此对照报告里的绝对值可能与单次跑批报告的聚合值不同。

## 历史夜间门禁（仅旧报告）

`gate.py` 是已退役 PostgreSQL retrieval/generation 报告的兼容层，不识别当前 Cowork
报告，也不应为当前架构生成新 snapshot。下列命令只用于重现历史实验；新的三轨统一门禁
使用本文顶部的 `eval.regression`，baseline readiness 以 `eval.catalog doctor` 为准。

检索轨与生成轨**各一份快照**，按报告类型自动解析，不用每次手写 `--baseline`：

| 轨 | 快照 | 从哪次跑批导出 |
|---|---|---|
| retrieval | `eval/snapshots/retrieval.json` | `suite_retrieval_runner` 的 `<batch>/<chunk_strategy>/report.json` |
| generation | `eval/snapshots/generation.json` | `suite_generation_runner` 的 `<batch>/<chunk_strategy>/report.json` |

两条 runner 都会把分 dataset 的子报告并成一份整套报告；门禁判的是**整套 70 条**，
不是四份分 dataset 报告分别判——那样配对样本会碎成四组，快照也要维护四份。

```bash
# 1. 从一次可信的跑批导出 baseline 快照，提交进 git（输出路径按轨自动选）
PYTHONPATH=backend backend/.venv/bin/python -m eval.gate snapshot \
  eval/outputs/dev-suite-retrieval/<run>/heading

# 2. 用候选跑批比对。--against 从该 git ref 读快照，所以在分支上也能对着 main 判
PYTHONPATH=backend backend/.venv/bin/python -m eval.gate check \
  eval/outputs/dev-suite-retrieval/<run>/heading --against main \
  --output-dir eval/outputs/gate/<label>
```

**快照只含数字与 UUID，不含任何原文**：字段走白名单（不是黑名单），
`answer` / `gold_answer` / `citations[].title` / `span_diagnostics[].quote` /
`source_uri` / `document_id` 一律不留，所以它可以提交进 git 而不触碰约束 7。
将来给报告加字段时新字段默认不进快照，不会悄悄把原文带进版本库。
gold span 的身份改存 sha256 前 16 位指纹——挡得住重标与解析版本漂移，也不泄露 quote。

判定规则（阈值依据见 [docs/06 §4.2](../docs/06-评测体系.md)）：

| 规则 | 条件 |
|---|---|
| `no_regression` | 规则轨指标的**聚合值**不许回退。逐样本胜负只报不拦——按逐样本拦会把净收益的改动也拦下来 |
| `no_comparable_samples` | 某个受门禁指标一条可配对样本都没有 → 判不合格，不是"这项跳过" |
| `cost_increase` | token 口径上涨 ≤ 20%（实测噪声最坏 8.8%）；算不出来就不放行 |

fail-closed：候选跑批含失败样本、gold span 指纹与 baseline 不一致、数据集不一致、
受控配置漂移，一律**拒绝判定**（退出码 2），与"判为不合格"（退出码 1）分开。
门禁刻意不提供 `--allow-config-drift` 这种后门。

> 想让一次有取舍的改动通过，正确动作是重新生成 baseline 快照并把取舍写进台账，
> 不是放宽阈值。门禁的作用是逼取舍显式化。

## badcase 棘轮

修复不是终点。可复现的 badcase 除了进 `regression` 评测集，还要在
`backend/tests/test_regression_badcases.py` 的 `BADCASES` 里登记，并指明哪几条用例挡住它。
元测试会把 `covered_by` 解析成真实的测试函数，**改名或删掉就直接红**。
两条路径不互相替代：评测集量的是指标，pytest 挡的是回归，而且每个 PR 都在跑。

## E1 四策略生成轨

检索轨证明"哪套分块更容易把 gold span 捞回来"，生成轨证明"捞回来之后答案与引用是不是更好"。
两轨必须同源，所以生成跑批以**检索轨的 manifest 为基准**，继承 dataset、origin、Top-K、
theta 与检索链路，只让 `chunk_strategy` 变：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.generation_strategy_runner \
  --manifest eval/outputs/chunk-strategies/<batch>/manifest.json \
  --label e1-generation-core-dev
```

跑批前置校验（任一不满足直接中止，不产出任何 run 或 manifest）：

| 检查 | 拒绝理由 |
|---|---|
| 检索 manifest 缺策略或缺字段 | 不是完整的四策略批次 |
| 当前 embedding 身份 ≠ manifest 记录 | 两轨读的不是同一份向量 |
| 语料指纹漂移 | 中间重建过 chunk，端到端结论无法与检索轨对齐 |
| gold span 指纹漂移 | 样本在两轨之间被改过 |
| gold answer / constraints 指纹在批内漂移 | `constraint_pass` 的判据被换过 |
| 四个 run 的受控配置不一致 | 不是单变量对照（回读数据库里的 config，不信内存变量） |
| 检索链路生成轨无法复现（如 `lexical-only`） | 同名却不同源，比不了 |

`prompt_fingerprint`（system prompt 的 sha256）与数据集、标注指纹一起进 `config_hash`：
改了 prompt 就必须重跑，不能复用旧 run 冒充同一条件下的对照。同一份配置重复执行会
**复用已完成的 run**，不重复烧钱；复用时不会导出 `report.json`，需要报告请加 `--no-reuse`。

逐条用量按 `llm_calls.eval_run_id + trace_id` 归集（`trace_id = <run_id>:<item_id>`），
覆盖一条样本触发的全部模型调用。注意是 `eval_run_id` 不是 `run_id`——后者外键指向
`agent_runs`，塞评测 run 会直接违反外键。

## 多策略对照矩阵（E1 四分块策略）

`strategy_matrix.py` 把上面的配对推广到 N 个策略，以其中一个为基线，
同样只读已完成跑批导出的 `report.json`：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.strategy_matrix \
  --manifest eval/outputs/chunk-strategies/<batch>/manifest.json \
  --baseline heading \
  --output-dir eval/outputs/strategy-matrix/<label>
```

manifest 模式会严格要求 `fixed/heading/recursive/semantic` 四套报告齐全，并自动把
`chunk_strategy` 与对应的 `chunk_metadata` 声明为受控变量；其他检索配置仍必须完全一致。
如 runner 复用了历史 run 而 manifest 中没有报告路径，请用 `--no-reuse` 重跑以导出报告。

`--generation` 可选，但给了就必须覆盖全部策略，否则端到端指标比较的是不同子集。
四策略生成轨可以直接用 `--generation-manifest` 传 `generation_strategy_runner` 的
manifest，效果等同于逐个写 `--generation`（两者只能给一个）：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m eval.strategy_matrix \
  --manifest eval/outputs/chunk-strategies/<batch>/manifest.json \
  --generation-manifest eval/outputs/generation-strategies/<batch>/manifest.json \
  --baseline heading \
  --output-dir eval/outputs/strategy-matrix/<label>
```

**fail-closed —— 下面任一条不满足直接拒绝出报告，不降级成"能算多少算多少"**：

| 检查 | 拒绝理由 |
|---|---|
| 数据集不一致 | 跨数据集比较无效 |
| item_id 集合不一致 / 有重复 | 无法严格配对 |
| category 或 answerable 逐条不一致 | 标注已漂移 |
| **gold span 指纹不一致** | version + 字符区间 + quote 有差异，说明混了解析版本或重标过 |
| 跑批含失败样本（`error` / `error_count`），或可答题缺检索指标 | 结果不完整 |
| 受控检索配置不一致 | 不是单变量对照；确属被对照的变量用 `--vary-key` 显式声明 |
| **受控生成配置不一致** | 模型 / prompt 指纹 / token budget / 阈值有差异，端到端差异无法归因到分块 |
| **生成报告的 `chunk_strategy` 与策略名对不上** | 报告挂错位置，四策略结论会整体错位 |
| 主指标没有任何公共可比样本 | 对照无意义 |

指标覆盖 span recall（Top-K 与固定 token budget 两个口径）、nDCG、α-nDCG、MRR、
context precision、**上下文冗余率**、**检索上下文 token**、拒答正确率、检索延迟，
以及可选生成轨的 constraint_pass、citation_validity、引用对齐、端到端延迟与成本。

口径要点：

- 每个指标取**所有策略的公共可比样本**（交集）。只要一个策略在某条样本上不适用，
  该样本从这个指标的所有策略里一起剔除，否则矩阵的列不同源，横向比较没有意义。
- 冗余率 = Top-K chunk 字符区间的重复覆盖占比（按 `version_id` 分组，跨版本不算重叠），
  越低越好；它把"大 chunk 靠包住 gold span 拿高 recall"的代价显性化。
- **成本**分两个口径。检索侧是上下文 token；生成侧由 `generation_baseline` 按
  `llm_calls.eval_run_id + trace_id` 归集逐条用量，覆盖一条样本触发的全部调用
  （query embedding、证据门控、正文生成），落在报告的 `total_tokens`。
  金额只在价格表非 0 时才报；本机模型自部署价格为 0，`cost_usd` 一律标
  `unavailable` 并给出原因，**不写成 0.00**——"没有可用价格"和"测过、就是不要钱"是两回事。
- 端到端质量只含规则轨；语义正确性需要校准过的 Judge，M0 尚不具备。
- 报告同时给出逐样本**胜/负/平**计数与**区间是否跨零**：前者是方向，后者才是显著性判定。
- 异常样本单列：策略间分歧最大的、所有策略都未命中的（应归因语料或标注）、
  所有策略都满分的（该样本对本次对照没有分辨力）。

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
