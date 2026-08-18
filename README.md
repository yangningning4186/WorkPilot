# WorkPilot · 个人 AI 知识副驾

> 把你读过、存过、想过的一切，变成**可溯源的问答**和**可执行的任务**。

一个人从 0 到上线的全栈 AI 应用。当前实现聚焦 RAG、固定 Agent、owner 长期记忆、
评测体系与成本治理；知识图谱和主动关联仍保留为后续蓝图，不把设计稿冒充已上线能力。

**作者是第一个也是最重的用户——每天真实在用。**

---

## 这个项目在解决什么问题

知识工作者的真实困境：几百篇存了没读完的 PDF、散在 Obsidian 里的笔记、
浏览器几千个书签、三个月前读过但只记得"好像看过"的那篇论文。

信息在增长，能被再次调用的部分却在萎缩。

WorkPilot 提供四种能力：

| 能力 | 状态 | 例子 | 硬要求 |
|---|---|---|---|
| **问** | ✅ 已实现 | "我之前看的那篇讲对比学习的论文，负样本是怎么构造的？" | 答案带引用，精确到文件 + 页码，可点击跳原文；找不到就**说找不到**，不编 |
| **做** | ✅ 固定综述已实现 | "把我这个月读的 8 篇 RAG 论文整理成综述，按方法分类，标出彼此差异" | 流程固定，先确认再写回；步骤可见、可中断、可从 checkpoint 恢复 |
| **记住** | ✅ owner 长期记忆已实现 | "以后回答先给结论，再补依据" | 仅登录 owner 抽取与召回；可查看、编辑、删除、置顶和恢复历史版本 |
| **编辑** | ✅ 本地办公工作台已实现 | "把这份 Word 的结论改得更精炼，并更新 Excel 汇总公式" | owner 先授予限时权限；权限内直接写入 `.md` / `.docx` / `.xlsx`，有冲突检测、备份和原子替换 |
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
| 前端 | Next.js 16 (App Router) · React 19 · TypeScript · 原生 CSS · react-markdown · 自写 SSE 客户端 |
| 后端 | Python 3.12 · FastAPI · Pydantic · SQLAlchemy · Arq |
| Agent | LangGraph 固定 `literature_review` 状态机 · PostgreSQL checkpoint · owner-only HITL · 幂等写回 |
| 记忆 | 两阶段事实抽取 · ADD/UPDATE/DELETE/NOOP · 时序有效性 · pgvector 召回 · owner-only `/memory` |
| 办公编辑 | owner 会话限时授权 · Markdown / python-docx / openpyxl 格式执行器 · 冲突保护 · 恢复副本 · 原子写入 |
| Cowork 桌面 | Tauri 2 · 随机 localhost sidecar · 会话 root/network capability · 文件/搜索/网页/PDF/Office/Artifact · 策展式 MCP client · 渐进加载 Skill · Progress / Artifacts |
| 知识 | MinerU / PyMuPDF · bge-m3 dense 向量 · PostgreSQL + pgvector · PG 词法检索 · bge-reranker-v2-m3 |
| 模型 | 统一网关 · `light/main/heavy/external` 路由与 fallback · 置信度升档 · Redis 精确缓存 |
| 评测 | span-level 标注 · 规则轨 + Judge · paired bootstrap · weighted Kappa · 两层 CI 门禁 |
| 基础设施 | PostgreSQL + pgvector · Redis · OrbStack / Docker Compose |

### 长期蓝图（尚未实现）

多 Agent 委派、个人知识图谱、每日 digest、语义缓存、
sparse 第三路检索、Obsidian/Zotero/web_clip connector、Langfuse/OpenTelemetry、MinIO 和公网部署
都仍在 [MVP Backlog](docs/11-MVP边界.md#5-backlog按解锁顺序) 或最终交付清单中。

### 启动 Cowork 桌面版

先启动 `deploy/` 中的 PostgreSQL 与 Redis，并安装 Rust stable 以及当前平台的
Tauri 2 系统依赖，然后：

```bash
cd frontend
npm ci
npm run dev:desktop
```

桌面壳会自动选择随机本机端口，生成当次启动 token，执行 Alembic 迁移，
并同时启动 FastAPI 与 Arq worker。目录必须经系统选择器授权；授权后该会话
对目录内的通用文本和 Word / Excel 可直接读写，PDF 可受控读取，并能生成 Artifact；
这些操作在目录授权后不再逐条弹确认。
读取公开网页/远程 PDF 需要在当前 Cowork 会话内额外授予 `network.read`。

MCP client 配置示例见 [`config/mcp.yaml.example`](config/mcp.yaml.example)。复制为
`config/mcp.yaml` 后，先通过 `POST /api/v1/integrations/mcp/{server}/probe` 审阅并固定
远端工具目录哈希；未逐项启用、目录发生漂移或声明有副作用的工具都不会进入 Cowork。
当前没有逐字段内容污点追踪，因此 `data_scope: deny` 同样保持不可见，必须由管理员显式
设为 `corpus_allowed` 才允许数据出站。
本地 Skill 放在 `skills/<name>/SKILL.md`，格式与边界见 [`skills/README.md`](skills/README.md)。

---

## 文档索引

| 文档 | 内容 |
|---|---|
| [01 总体规划](docs/01-总体规划.md) | 定位、场景、成功标准、非目标、风险登记册 |
| [02 架构设计](docs/02-架构设计.md) | 分层架构、请求时序、目录结构、选型理由与替代方案 |
| [03 数据模型](docs/03-数据模型.md) | 全部表结构、索引策略、pgvector 参数 |
| [04 RAG 设计](docs/04-RAG设计.md) | 解析、分块、混合检索、rerank、溯源、拒答 |
| [05 Agent 设计](docs/05-Agent设计.md) | 状态机、工具规范、三层记忆、反思、预算熔断 |
| [06 评测体系](docs/06-评测体系.md) | 评测集分层、指标定义、Judge 校准、CI 门禁 |
| [07 模型路由与成本](docs/07-模型路由与成本.md) | 三档路由、网关设计、两个必做实验、三级缓存 |
| [08 前端设计](docs/08-前端设计.md) | 页面结构、SSE 协议、边界情况清单、AI 辅助开发流程 |
| [09 排期与任务清单](docs/09-排期与任务清单.md) | 6 周逐周可勾选任务、每周交付物与"可讲的数据" |
| [10 简历与面试](docs/10-简历与面试.md) | 简历模板、必答题清单、诚实清单 |
| **[11 MVP 边界](docs/11-MVP边界.md)** | **唯一约束开发范围的文档**，含 Backlog 与解锁顺序 |
| [12 安全与部署](docs/12-安全与部署.md) | 威胁模型、鉴权限流费用熔断、SSRF、上线检查清单 |
| [13 办公工作台与本地文档编辑](docs/13-办公工作台与文档编辑.md) | 限时写权限、Markdown/Word/Excel 格式执行器、备份与冲突保护 |
| [15 桌面 Cowork 架构与开发基线](docs/15-桌面Cowork架构与开发基线.md) | 会话授权、通用工具循环、网页/PDF、Artifact 与桌面安全基线 |
| [ADR](docs/adr/) | 架构决策记录 |
| [实验台账](docs/experiments/) | 每次优化的"改了什么 → 指标怎么变" |

开发约定见 [CLAUDE.md](CLAUDE.md)。

---

## 项目状态

**状态快照：2026-08-18。** M0 已收口；M1 的检索、固定 Agent 与六类 binary correctness
Judge 校准已完成；三档路由主线收口后，owner 长期记忆已按明确授权从 Backlog 解锁。

### 已实现

- 入库、流式问答、引用高亮、证据充分性拒答、鉴权限流、混合检索、rerank 与四策略对照
  （数值分数门已按排序器分源，待固定分数源校准后再启用）
- 固定综述 Agent：六步状态机、三维预算、checkpoint、`SIGKILL` 恢复、HITL 与 effectively-once 写回
- `light/main/heavy/external` 路由、fallback、确定性升档、精确缓存、GPU 批次成本口径与 admin 成本看板
- owner 长期记忆：登录后异步抽取、四操作时序更新、相关记忆召回注入，以及 `/memory` 管理与历史恢复；匿名 demo 全路径隔离
- owner 本地办公工作台：授权后按自然语言指令直接修改 `.md`、`.docx`、`.xlsx`，具备 source 路径约束、外部修改冲突检测、恢复副本与原子替换；当前不是通用 Office 或多 Agent 系统
- 80 条 human 评测集（70 dev + 10 隔离 test）、严格配对 diff、bootstrap 与 badcase 棘轮
- 夜间 gate 已在**检索轨与生成轨**双双点亮：两份 baseline 快照均已提交，
  检索轨的通过 / 阻断 / 拒判三条路径各用真报告实跑验证过
- 独立 validation 19 条上，heavy Judge accuracy/QWK 为 **0.9474/0.8725**，
  main 为 **1.0000/1.0000**；日常 binary correctness Judge 采用 main
- 当前验证：后端与前端测试、Ruff、mypy、ESLint、TypeScript 全部通过

### 已有数据，但结论有边界

- 人工引用准确率 **95.45%（42/44）**；修复后不可答题 **13/13** 正确拒答，
  可答题实际回答从 36/57 提升到 44/57，仍有 13 条误拒
- Judge 校准只覆盖当前六类的 `answer-correctness-binary.v2`；类别 validation 仅 2–5 条，
  不能外推到逐类可靠性、faithfulness、citation accuracy 或 `agent_task`
- heavy 的 validation QWK 点估计过门，但 95% CI 下界为 0.5674；main 的 19/19 也不能
  读成真实总体准确率必然为 100%
- HNSW 调参在当前 40 篇语料规模下未真正命中向量索引；语料扩容后必须重跑
- A5 paired runner 与合成工程种子已就绪；真实模型跑批因端点信任边界未确认而未执行，
  合成样本不得冒充产品增益，owner 满意度仍待盲评

### 收口中

- 扩充并人工复核 `agent_task`，再从六类中间结论升级为七类正式校准
- nightly dev + Judge：两条规则轨的门禁已生效，但 `answer_correctness`
  接进生成轨快照与夜间定时化都还没做
- 保持 10 条 test 隔离，最终里程碑只运行一次
- README 完整使用说明、前端打磨、安全清单、独立演示环境、博客、视频和公网部署

- 开发范围以 [11 MVP 边界](docs/11-MVP边界.md) 为准（设计文档描述完整蓝图，含 Backlog 内容）
- 逐周进度见 [09 排期与任务清单](docs/09-排期与任务清单.md)
