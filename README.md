# WorkPilot · 桌面 AI 知识副驾

> **OpenWorker 的手 + DeepTutor 的眼睛**：一个能读懂你的文档、并且据此动手的本机 Agent。

两个参照物做的是同一件事的两半——OpenWorker 有一个能在你自己硬盘上动手的工具循环，
但对"依据"没有承诺；DeepTutor 每下一个论断就把阅读器滚到依据那一页，但读完之后什么也
做不了。合起来才是这个产品。

它不是把两个应用装在一起：阅读是同一个 run 的一档工作模式，和办公档共用一套工具循环、
审批、预算与 checkpoint。所以"读完这篇论文，把结论写进我的周报"是**一次** run。

**跑在你自己的机器上，没有容器，没有外部服务，没有一个字节离开本地**——除了你自己配的
那个模型端点。

**作者是第一个也是最重的用户——每天真实在用。**

---

## 这个项目在解决什么问题

知识工作者的真实困境：几百篇存了没读完的 PDF、散在 Obsidian 里的笔记、
浏览器几千个书签、三个月前读过但只记得"好像看过"的那篇论文。

信息在增长，能被再次调用的部分却在萎缩。

WorkPilot 提供四种能力：

| 能力 | 状态 | 例子 | 硬要求 |
|---|---|---|---|
| **读**（阅读档） | ✅ 工具已实现，**面板未落地** | "这篇第三节到底在论证什么？" | 论断后跟 `[p.12]`，阅读器滚过去并高亮；**没读过就是不知道**——搜索片段不能当引文 |
| **问**（知识库） | ✅ 已实现 | "我读过的论文里，谁在做负样本构造？" | 一个 KB 就是一个目录；FAISS + BM25 两路 RRF，引用到文件 + 页码；找不到就**说找不到**，不编 |
| **做** | ✅ 已实现 | "把我这个月读的 8 篇 RAG 论文整理成综述，按方法分类" | 模型自己维护任务清单；可选计划模式先出方案再批准；每步可见、可中断、可从 checkpoint 恢复 |
| **编辑** | ✅ 已实现 | "把这份 Word 的结论改得更精炼，并更新 Excel 汇总公式" | 会话级目录授权；权限内直接写入 `.md` / `.docx` / `.xlsx`，有冲突检测、备份和原子替换 |
| **记住** | ✅ 已实现 | "以后回答先给结论，再补依据" | global / workspace / conversation 三级作用域；**不覆盖，只失效**——改写留下历史 |
| **无人值守** | ✅ 已实现 | "每天早上七点跑一遍 CI 并把失败摘要发到群里" | 单次/cron 计划、离线补跑一次、重叠保护；需要人时安全暂停进 Inbox |
| **想起** | 🔭 长期蓝图 | "今天这篇和三个月前那篇有什么关联" | 依赖尚未实现的知识图谱与每日 digest |

---

## 项目的核心主张

**功能宽度不是价值，可测量的质量才是。**

所以这个仓库里最重要的不是 `backend/` 或 `frontend/`，而是 `eval/` 和 `docs/experiments/`——
每一次检索策略调整、prompt 改写、模型降档，都有对照实验和数据留档，
**包括那些让指标变差的尝试**。

而"面向个人"带来一个别的项目买不到的东西：**评测数据是真的**。
语料是我自己读过的资料，所以我能亲手标注 gold answer；
badcase 来自我每天的真实使用，不是编出来的测试用例。

---

## 当前实现的技术栈

| 层 | 已实现选型 |
|---|---|
| 桌面 | Tauri 2 · 随机 localhost sidecar + 每次启动注入的 token |
| 前端 | Next.js 16 (App Router) · React 19 · TypeScript · 原生 CSS · react-markdown · 自写 SSE 客户端 |
| 后端 | Python 3.12 · FastAPI · Pydantic |
| Agent | LangGraph 两节点工具循环（`decide ⇄ execute_tools`）· checkpoint · 三维预算 · 逐次审批 · `tool_invocations` 幂等租约 |
| 阅读 | locator 寻址（PDF 按页 / 文本按节）· 三层匹配 · 引文校验回 block 级 bbox · `reader_goto` 驱动阅读器 |
| 知识库 | MinerU / PyMuPDF 解析 · LlamaIndex + FAISS + BM25 两路 RRF · manifest embedding 签名 · 一个 KB 一个目录 |
| 记忆 | 两阶段事实抽取 · 时序有效性（不覆盖只失效）· 三级作用域 · 模型按 id 改写 |
| 办公编辑 | 会话级目录授权 · Markdown / python-docx / openpyxl 格式执行器 · 冲突保护 · 恢复副本 · 原子写入 |
| 工具面 | 文件/搜索/Shell（含后台任务）/只读 git/网页/受控浏览器/Office/Artifact · 策展式 MCP client · 渐进加载 Skill · Scheduler / Unattended Inbox · 飞书消息面 |
| 模型 | 统一网关 · OpenAI / Anthropic / Gemini / DeepSeek / Qwen / Ollama · 会话级模型切换 · `light/main/heavy/external` fallback · Fernet 密文 + 库外 0600 主密钥 |
| 评测 | 规则轨 + Judge · paired bootstrap · weighted Kappa · 快照门禁（⚠️ 检索轨当前是断的，见下） |
| 存储 | **SQLite + JSONL + 目录**。没有 PostgreSQL、没有 Redis、没有容器 |

### 长期蓝图（尚未实现）

阅读器面板前端、可写多 Agent 委派、个人知识图谱、每日 digest、持久化批注、
Obsidian/Zotero/web_clip connector 仍在
[MVP Backlog](docs/11-MVP边界.md#5-backlog按解锁顺序)。

**明确不做的**：语义缓存（错误命中会安静地返回"看起来对"的答案，在一个把接地当核心承诺
的产品里不划算）、Langfuse（桌面产品把 trace 发到第三方需要单独征得同意）、公网 demo
（产品价值在于读用户自己机器上的文件，公网做不到也不该做到）。

### 启动 Cowork 桌面版

**不需要启动任何外部服务**。安装 Rust stable 以及当前平台的 Tauri 2 系统依赖，然后：

```bash
cd frontend
npm ci
npm run dev:desktop
```

桌面壳会自动选择随机本机端口，生成当次启动 token，并启动 FastAPI 与嵌入式 worker。
状态全部落在 `~/.workpilot/` 下（`cowork.db` / `conversations/` / `telemetry.db` / `kb/`），
首次运行自动创建，无需迁移。普通任务的新交付物默认写入本机 `~/Documents/WorkPilot`；
读取或改写其他本机目录时再经系统选择器授权。授权后的目录可直接读写通用文本和
Word / Excel，PDF 可受控读取，并能生成 PPTX / DOCX / XLSX / PDF 等 Artifact；
这些操作在目录授权后不再逐条弹确认。
读取公开网页/远程 PDF 需要在当前 Cowork 会话内额外授予 `network.read`。

MCP 服务可以直接在桌面端 MCP 页面新增、探测、固定目录、绑定 OAuth 连接器并逐工具配置；
配置示例仍见 [`config/mcp.yaml.example`](config/mcp.yaml.example)。未逐项启用或目录发生漂移
的工具不会进入 Cowork；副作用工具只有声明为逐次审批后才可启用，并走统一调用租约。
当前没有逐字段内容污点追踪，因此 `data_scope: deny` 同样保持不可见，必须由管理员显式
设为 `corpus_allowed` 才允许数据出站。
本地 Skill 放在 `skills/<name>/SKILL.md`，桌面端支持安装、更新、启停、删除和 ZIP 导入，
格式与边界见 [`skills/README.md`](skills/README.md)。

顶部“自动化”入口可以为已有 Cowork 会话创建单次或五段 cron 计划。计划在本机 worker
中派发；应用重启后对错过的时间点最多补跑一次，同一会话已有运行中或等待人工处理的任务时
跳过本轮，避免任务堆叠。计划运行需要补充信息、申请目录/能力或审批 Shell 时会安全暂停并
进入 Unattended Inbox；无人值守模式不会自动续期授权，也不会自动批准高风险动作。

---

## 文档索引

| 文档 | 内容 |
|---|---|
| [01 总体规划](docs/01-总体规划.md) | 定位、场景、成功标准、非目标、风险登记册 |
| [02 架构设计](docs/02-架构设计.md) | 分层架构、请求时序、目录结构、选型理由与替代方案 |
| [03 数据模型](docs/03-数据模型.md) | SQLite 表结构、JSONL 消息、目录即真相、三种删除语义 |
| [04 知识与阅读设计](docs/04-知识与阅读设计.md) | 解析、locator 寻址、引文校验、本地 KB 混合检索、溯源、拒答 |
| [05 Agent 设计](docs/05-Agent设计.md) | 状态机、工具规范、三层记忆、反思、预算熔断 |
| [06 评测体系](docs/06-评测体系.md) | 评测集分层、指标定义、Judge 校准、CI 门禁 |
| [07 模型路由与成本](docs/07-模型路由与成本.md) | 三档路由、网关设计、两个必做实验、缓存与成本口径 |
| [08 前端设计](docs/08-前端设计.md) | 页面结构、SSE 协议、边界情况清单、AI 辅助开发流程 |
| [09 排期与任务清单](docs/09-排期与任务清单.md) | 6 周逐周可勾选任务、每周交付物与"可讲的数据" |
| [10 简历与面试](docs/10-简历与面试.md) | 简历模板、必答题清单、诚实清单 |
| **[11 MVP 边界](docs/11-MVP边界.md)** | **唯一约束开发范围的文档**，含 Backlog 与解锁顺序 |
| [12 安全与部署](docs/12-安全与部署.md) | 威胁模型、鉴权限流费用熔断、SSRF、上线检查清单 |
| [13 办公工作台与本地文档编辑](docs/13-办公工作台与文档编辑.md) | 限时写权限、Markdown/Word/Excel 格式执行器、备份与冲突保护 |
| [15 桌面 Cowork 架构与开发基线](docs/15-桌面Cowork架构与开发基线.md) | 会话授权、通用工具循环、网页/PDF、Artifact 与桌面安全基线 |
| [16 两个参照物的对齐](docs/16-OpenWorker-P0-P1对齐.md) | OpenWorker 侧与 DeepTutor 侧的实现矩阵，以及**刻意保留的分歧** |
| [ADR](docs/adr/) | 架构决策记录 |
| [实验台账](docs/experiments/) | 每次优化的"改了什么 → 指标怎么变" |

开发约定见 [CLAUDE.md](CLAUDE.md)。

---

## 项目状态

**状态快照：2026-08-22。** 形态从"要发公网 demo 的 Web 服务"转成**桌面应用**：
PostgreSQL / Redis / Arq / pgvector / MinIO / Langfuse 全部退役（净删 2.5 万行），
沉浸阅读并成一档工作模式。两条 ADR 记录了这次转向：
[0012](docs/adr/0012-退役postgres与redis改用本机文件.md)、
[0013](docs/adr/0013-沉浸阅读作为工作模式而非第二条产品线.md)。

### 已实现

- **工具循环**：两节点 LangGraph、三维预算、checkpoint、`SIGKILL` 恢复、
  `tool_invocations` effectively-once 幂等、空转熔断、上下文压缩、自唤醒
- **审批三档**：计划模式（只读）· 逐次审批（默认）· 免审批；常驻规则只省"再问一次"，
  **不放大 capability**
- **阅读**：locator 寻址、三层匹配、引文校验回 bbox、`reader_goto`
  （⚠️ 阅读器面板前端未落地）
- **知识库**：一个 KB 一个目录，FAISS + BM25 两路 RRF，manifest embedding 签名挡住
  "换了模型但索引是旧的"这种无声失败；挂载后有确定性预检索
- **记忆**：三级作用域、时序有效性（不覆盖只失效）、抽取作业与记忆同库同事务
- **本地办公**：授权后直接修改 `.md`、`.docx`、`.xlsx`，冲突检测、恢复副本、原子替换
- **无人值守**：单次/cron 计划、离线补跑一次、重叠保护、跨会话 Inbox、飞书镜像
- **成本**：`light/main/heavy/external` 路由、fallback、确定性升档、进程内精确缓存、
  GPU 批次摊销口径、每日费用闸门（整数微美元）
- **测试**：644 个用例，`docker compose down` 之后照常全绿
- 夜间 gate 已在**检索轨与生成轨**双双点亮：两份 baseline 快照均已提交，
  检索轨的通过 / 阻断 / 拒判三条路径各用真报告实跑验证过
- 独立 validation 19 条上，heavy Judge accuracy/QWK 为 **0.9474/0.8725**，
  main 为 **1.0000/1.0000**；日常 binary correctness Judge 采用 main
- 当前验证：后端与前端测试、Ruff、mypy、ESLint、TypeScript 全部通过

- 人工引用准确率 **95.45%（42/44）**；修复后不可答题 **13/13** 正确拒答，
  可答题实际回答从 36/57 提升到 44/57，仍有 13 条误拒
- Judge 校准只覆盖当前六类的 `answer-correctness-binary.v2`；类别 validation 仅 2–5 条，
  不能外推到逐类可靠性、faithfulness、citation accuracy 或 `agent_task`
- heavy 的 validation QWK 点估计过门，但 95% CI 下界为 0.5674；main 的 19/19 也不能
  读成真实总体准确率必然为 100%
- HNSW 调参在当前 40 篇语料规模下未真正命中向量索引；语料扩容后必须重跑
- A5 paired runner 与合成工程种子已就绪；真实模型跑批因端点信任边界未确认而未执行，
  合成样本不得冒充产品增益，owner 满意度仍待盲评

### ⚠️ 当前欠的债（按优先级，不藏着）

1. **检索评测轨是断的。** 四层表（`documents / versions / parsed_blocks / chunks`）随
   PostgreSQL 退役，`span_recall` 那组指标目前没有可跑的实现；`eval/cowork_runner.py`
   还硬要求 `COWORK_STORE_BACKEND=postgres`，也就是说**唯一还在长的那个任务套件跑不起来**。
   这违反了约束 6（改了影响输出的逻辑就要同步补评测）。处置见
   [06 §4.5](docs/06-评测体系.md)。
2. **阅读器面板前端没落地。** 工具已经在发翻页指令，但没有地方显示。
3. **阅读一条评测都没有。** `locator_accuracy` / `quote_verifiability` /
   `read_before_claim` 三个指标已定义、未实现。
4. **请求层与会话层限流已随 Redis/PG 一起删掉**，没有替代。桌面形态下不是漏洞
   （只监听 localhost + 启动 token），但要上公网就是阻塞项（[12 §2.2](docs/12-安全与部署.md)）。
5. 约 190 处惰性 `session` 形参待摘（`app/core/db.py` 现在 `execute()` 直接抛错，
   防止有人偷偷把 SQL 带回来）。
6. **拿什么替代公网 demo，尚未决定**（[02 §5](docs/02-架构设计.md) 三选一）。

### 转向之前已有的数据，结论仍然有边界

- 开发范围以 [11 MVP 边界](docs/11-MVP边界.md) 为准（设计文档描述完整蓝图，含 Backlog 内容）
- 逐周进度见 [09 排期与任务清单](docs/09-排期与任务清单.md)
