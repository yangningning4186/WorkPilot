# 04 · RAG 设计

**核心承诺**：答案必须可溯源到原文页码，证据不足时拒答。
所有设计围绕这一条展开，任何丢失溯源元数据的环节都是 bug。

> **范围提示**：M0 只做 **PDF/Markdown 解析 + `heading` 分块 + dense 检索 + 拒答**。
> 混合检索、rerank、查询改写、四策略对照是 M1；全局摘要在 Backlog。
> 详见 [11 MVP 边界](11-MVP边界.md)。

---

## 1. 解析：解析即结构化

> 借鉴 RAGFlow DeepDoc — 不把 PDF 当纯文本流，先做版面分析得到 block 序列，再按类型分流。

```
原始文件
  │
  ├─ 版面分析（MinerU，中文/学术 PDF 首选；Docling 兜底）
  │    └─→ block 序列：{type, content, char span, locations[]}
  │        type ∈ title|paragraph|table|list|figure|formula|header|footer|code
  │
  ├─ 按类型分流处理
  │    ├ title      → 构建标题层级树（heading_path 的来源）
  │    ├ paragraph  → 清洗（去连字符断行、合并跨页/跨栏段落）
  │    ├ table      → 转 Markdown，**处理跨页表格合并**，保留表头
  │    ├ formula    → 转 LaTeX；行内公式保留在段落中，避免切碎段落
  │    ├ figure     → 提取 caption；无 caption 用视觉模型生成一句描述
  │    └ header/footer → **丢弃**
  │
  └─→ 归一化产物：
       document_versions.full_text  （字符偏移基准）
       parsed_blocks[]              （稳定最小单元，标注锚点）
         └─ parsed_block_locations[]   （一个 block 可跨页/多区域）
```

**学术 PDF 的两个特有难点**：
- **双栏排版**：阅读顺序不是自上而下，必须先做栏识别再定序，否则段落全乱
- **公式**：行间公式独立成 block，行内公式必须留在原段落——
  把 `$\mathcal{L}_{con}$` 切出去，这段话就读不懂了

**为什么 header/footer 必须丢**：论文每页都有会议名、arXiv 编号、页码。
不去掉的话每个 chunk 都被这段噪声污染，向量被拉向同一方向，检索区分度骤降。

### 1.1 定位元数据必须完整（不只是 bbox）

只存 `[x0,y0,x1,y1]` 四个数是不够的。PDF 原生坐标原点在**左下角**，
而 pdf.js 等前端渲染器用**左上角**；页面可能带旋转；不同解析器坐标基准不一致。

`parsed_blocks` 记录文本与字符区间，`parsed_block_locations` 记录一到多个位置：

| 字段 | 作用 |
|---|---|
| `bbox_norm` | 归一化到 `[0,1]`，前端按实际渲染尺寸缩放 |
| `page_width` / `page_height` | 解析时的页面尺寸（pt） |
| `rotation` | 0/90/180/270 |
| `coord_origin` | `top_left` / `bottom_left` |
| `parser` + `parser_version`（在版本表） | 坐标语义的解释依据 |

漏掉任何一项，换个渲染器高亮就会错位。
位置子表让跨页表格和多栏段落能够对应多个 bbox，而不是丢掉其中一页。

`char_start/end` 统一是 NFC 文本的 Unicode code-point offset。
前端 UTF-16 选区必须交给后端转换，并用 quote 校验，不允许混用字节、UTF-16 与 code-point 偏移。

### 1.2 解析质量校验

每批入库随机抽 **20 个 block** 人工检查，重点看表格是否错位、跨栏段落是否断裂、
公式是否被切出段落。解析质量单独建一组评测样本（给定 PDF 页 → 期望的 Markdown 表格），
**不混进问答评测**。

解析必须跑在独立子进程并设资源上限——MinerU 处理畸形 PDF 时 OOM 并不罕见
（[12 §2.3](12-安全与部署.md)）。

### 1.3 当前实现（M0）

- `auto` 路由先跑 PyMuPDF 轻量分析；检测到多栏、嵌入图片或文本质量问题时选择 MinerU。
- MinerU CLI 在独立进程组运行，整文档超时/取消会杀掉进程组；其 Python/模型环境与后端隔离。
- MinerU `content_list.json` 统一转换为 `title/paragraph/table/formula/figure_caption/list/code`，
  坐标从 0–1000 归一化到左上原点 `[0,1]`；页眉、页脚、页码、旁注和脚注不进入索引。
- 结构门控强制校验 block 顺序、Unicode 区间回切、页码、页面尺寸、旋转角和 bbox；文本门控检查
  空结果、低密度、替换字符、控制字符及定位覆盖率。MinerU 失败可配置回退 PyMuPDF。
- 版本去重身份包含实际 `parser + parser_version`；解析策略、分块或 embedding 身份另进入
  `source_sync_entries.ingest_signature`，避免配置升级后被文件 stat 快速跳过。
- `document_versions.parse_meta` 持久化实际 backend、路由理由、回退原因、耗时和质量指标，
  API 响应也回传这些字段，便于定位“谁解析的、为何选它、是否降级”。

质量跑批由 `eval/pdf_parsing_quality.py` 执行，输出机器可读 JSON、人工可读 Markdown 和抽样 bbox
叠图。跑批产物不提交，只把人工验收结论登记到 `docs/experiments/`。

---

## 2. 分块

### 2.1 chunk 由 block 组成

chunk **不是**对 `full_text` 的独立切分，而是对 `parsed_blocks` 序列的分组：

```
parsed_blocks:  [b0][b1][b2][b3][b4][b5][b6]...
                 └──── chunk#0 ────┘└─ chunk#1 ─┘
chunks 记录:  block_start_idx / block_end_idx  +  char_start / char_end
```

同时记 block 区间和字符区间，是因为超长 block（一整节正文）会被切开，
此时 chunk 只覆盖该 block 的一部分字符。

**这个结构是评测能跨策略比较的前提**（[ADR-0006](adr/0006-分块与标注分层.md)）：
gold 标注锚在字符区间上，任意策略的 chunk 都能通过区间重叠算出命中。

### 2.2 四策略对照（M1 实验 E1）

| 策略 | 实现 | 预期强项 | 预期弱项 |
|---|---|---|---|
| `fixed` | 512 token 定长，overlap 64 | 实现简单、块大小均匀 | 切断语义边界，表格被腰斩 |
| `recursive` | 按 `\n\n → \n → 。` 递归切分 | 尊重自然边界，工程默认解 | 长段落仍被硬切 |
| `semantic` | 相邻句 embedding 相似度骤降处切分 | 语义完整 | 慢、块大小方差大、**对结构规整的论文可能更差** |
| `heading` | 按标题层级树切，超长再递归细分 | **对论文/技术文档最优**，天然带 heading_path | 依赖解析质量，无标题文档（网页剪藏）退化 |

**M0 只实现 `heading`**——它是预判最优项，先用它跑通全链路。

**基线预判（待实验证伪）**：`heading` 在论文和技术文档上最好，
因为学术写作的章节结构本身就是作者做好的语义分块；
`semantic` 只在网页剪藏、随手笔记这类无结构文本上才有优势。
**如果实验推翻这个预判，那才是最值得写进博客的内容。**

### 2.3 统一规则

- `block_type = table` 的块不参与合并切分，整表作为一个 chunk（表格被切碎等于废掉）
- 每个 chunk 前置注入 `heading_path` + 文档标题作为上下文前缀
  （论文里"我们采用 InfoNCE 损失"脱离"3.2 负样本构造"就无法检索）
- 超过 bge-m3 上限（8192 token）强制细分

### 2.4 每策略独立的向量索引 ★

四策略共用一个 HNSW、查询时按 `strategy` 过滤，会导致 **召回退化**：
pgvector 的属性过滤发生在候选扫描阶段，约 75% 候选被丢弃，可能凑不满 top-k。
**E1 测出来的差异会混进索引退化噪声，结论是错的。**

正确做法是每策略一个**部分索引**，细节见 [03 §4.1](03-数据模型.md)。

---

## 3. 向量化

- 模型：**bge-m3**，同时产出 dense(1024 维) + sparse(词权重)
- 查询侧与文档侧同模型，注意 bge 系列查询端需加指令前缀
- 批量灌库 batch=32，本地部署无调用成本

### sparse 向量：生成即存，v1 不建检索路径

dense 与 sparse 是**同一次前向同时输出**的，存储成本接近零。
但 JSONB 上没有可用的高效稀疏检索索引，自己实现倒排与打分在 v1 不划算。

**决策**：存下来（否则将来要用得重跑全量 embedding），但 v1 检索只有两路：
**dense 向量 + 词法检索**。sparse 作为可选第三路进 [Backlog #6](11-MVP边界.md)。

---

## 4. 检索流水线（M1 完整形态）

```
用户 query
  │
① 查询改写 [light]
  │  ├ 指代消解："它的负样本怎么构造" → "SimCLR 的负样本怎么构造"
  │  ├ 多查询扩展：生成 3 个同义改写
  │  └ HyDE：生成假想答案再检索
  │
② 前置过滤（不是后置！）
  │  ├ 当前可见：is_searchable = true
  │  ├ 历史问题：activated_at <= temporal_ctx
  │  │            AND (invalid_at IS NULL OR invalid_at > temporal_ctx)
  │  └ 元数据：doc_type、tags、来源、时间范围
  │     → 后置过滤的坏处：top-k 被无关文档占满，符合条件的挤不进来
  │
③ 并发双路召回（各 top-50）
  │  ├ dense 向量  pgvector HNSW（部分索引 + iterative_scan）
  │  └ 词法检索    PG 全文索引 + zhparser
  │
④ RRF 融合 → top-30      score(d) = Σ_r 1/(k + rank_r(d))，k=60
  │
⑤ rerank [bge-reranker-v2-m3] → top-5
  │
⑥ 拒答判定：rerank 最高分 < τ → "资料库中未找到相关信息"
  │
⑦ 生成 [main]：强制句末输出引用标记，后处理解析成 citation 事件
```

### 4.1 命名纠正：PG 全文检索不是 BM25 ★

**PostgreSQL 原生全文检索用的是 `ts_rank` / `ts_rank_cd`（cover density 排名），
不是 BM25。** 两者在词频饱和、文档长度归一化上的处理完全不同。

把它叫 BM25 是**术语错误**，面试时会被抓——这正是懂行的人一听就知道
候选人是否真的读过检索这块的信号。

| 方案 | 说明 | 本项目选择 |
|---|---|---|
| `ts_rank_cd` + zhparser | PG 原生，零额外依赖，但**不是 BM25** | ✅ v1 采用，文档中一律称"**词法检索**" |
| `pg_textsearch` | Tiger Data 出品，真正的 BM25，2026 年开源 | 若 E2 显示词法路是瓶颈则换 |
| ParadeDB `pg_search` | 基于 Tantivy 的 BM25 扩展 | 同上，备选 |
| Elasticsearch | IK 中文分词更成熟 | ❌ 为一路检索引入整套 ES 不划算 |

**已知风险**：zhparser 中文分词质量弱于 IK，且 `ts_rank_cd` 弱于 BM25。
**这两处差距要在 E2 实验里量化，不能含糊带过。**

### 4.2 为什么 RRF 而不是加权求和

加权求和需要把不同检索器的分数归一化到同一量纲——
余弦相似度和 `ts_rank_cd` 分数的分布完全不同，归一化方式本身引入偏差，
且权重要按数据集调。RRF 只用**排名**不用分数，无量纲、无需调参、对异常分数鲁棒。

代价是丢弃了分数的绝对强度信息，所以 rerank 不能省——它把绝对相关性补回来了。

### 4.3 拒答阈值 τ

用 dev 集的 `unanswerable` 样本 + 可答样本画 ROC，选 **F1 最优点**，
并明确记录代价：τ 调高会让部分可答问题被误拒。这个权衡曲线本身就是面试素材。

M0 先拍一个保守值，M1（E7）做正式调优。

---

## 5. 溯源实现

引用的完整链路，任何一环断掉前端就无法定位原文：

```
解析      → parsed_blocks 存 char_start/end；parsed_block_locations 存多页 bbox 与坐标元数据
   ↓
分块      → chunks 记录覆盖的 block 区间与字符区间
   ↓
检索      → RetrievedChunk 透传 block_ids + 字符区间 + 分数，并展开成 block 级 evidence segments
   ↓
生成      → 每个 evidence segment 分配消息内短标签 [S1]/[S2]，要求答案句末引用
   ↓
后处理    → 用短标签映射回 block_id + overlap span，回查定位元数据并校验引用
   ↓
SSE       → citation 事件（独立事件，不阻塞正文流）
   ↓
前端      → 引用卡片 → 打开原文 → page_no 定位 → bbox_norm × 渲染尺寸 高亮
```

**引用锚定在 `block_id` 而非 `chunk_id`**：block 跨策略稳定，
换分块策略后，历史消息的引用依然能定位；重新解析会生成新版本，
历史引用继续指向原版本，除非执行物理清理。

短标签只用于减少 prompt token，不能成为持久化身份。消息必须把 `citation_id`（如 `S1`）
到 `block_id/version_id/char span/quote/locations[]` 的完整映射存入 `citations`；
模型输出未知标签或引用了未随本次 prompt 提供的 block 时，后处理必须判无效，不能猜测修复。

**引用准确率**是独立指标：模型可能引用了 block 但该 block 并不支撑答案（引用幻觉）。
判分方式：给 Judge 看「答案句 + 被引用的 block 原文」，判断是否真的支撑。

---

## 6. 全局性问题（Backlog #8）

向量检索天生答不好"我这三个月读的论文整体在关注什么"——答案不在任何单个 chunk 里。

> 借鉴 GraphRAG 的社区摘要思想（不引入整套 GraphRAG）：
> 对文档层级树做自底向上摘要，摘要也作为可检索 chunk（`block_type = summary`）。

评测集设 `global` 类别，用来展示**朴素 RAG 的失败**与解法对比——
"我知道我的方案在哪里不行，以及为什么"比"我的方案很强"更有说服力。

---

## 7. 指标定义

**指标必须分解到组件**，否则无法归因是检索坏了还是生成坏了。

### 检索层（span-level，跨策略公平）

```
chunk c 命中 gold span g  ⟺  overlap(c, g) / len(g) ≥ 0.5

Recall@k = |{g : ∃c ∈ top-k, c 命中 g}| / |gold_spans|
```

| 指标 | 说明 |
|---|---|
| `span_recall@k` | 标注的关键段落有几个被召回 |
| `ndcg@k` | `rel(c)=min(1, Σ_g overlap(c,g)/len(g))`，按位置折损 |
| `alpha_ndcg@k` | 将 gold span 视作 subtopic，对重复证据降权（α=0.5） |
| `mrr` | 第一个命中 chunk 的排名倒数 |
| `context_precision` | top-k 中真正相关的比例 |

> span-level 比 chunk-id 指标更贴近用户感知，但单独使用仍会奖励长 chunk。
> E1 以固定检索 token budget 为主口径，并同时报告 context precision、冗余率、
> α-nDCG 与端到端质量。详见 [06 §2.1](06-评测体系.md)。

### 生成层

| 指标 | 判分 |
|---|---|
| `answer_correctness` | Judge 对比 gold_answer |
| `faithfulness` | 每个论断是否被检索上下文支撑 |
| `citation_accuracy` | 引用的 block 证据片段是否真的支撑对应句子 |
| `refusal_correctness` | 不可答正确拒答 + 可答不误拒 |
| `format_compliance` | 结构化输出字段齐全度（规则轨） |

### 端到端

`latency_p50/p95`、`ttft`、`cost_per_query`

---

## 8. 实验路线图

按顺序做，**一次只改一个变量**。每个实验一份台账（`docs/experiments/`）。

| # | 实验 | 变量 | 里程碑 |
|---|---|---|---|
| E1 | 分块策略四选一 | strategy（同一份 gold_spans） | M1 |
| E2 | 混合检索增益 | dense → dense + 词法 + RRF | M1 |
| E3 | rerank 值不值 | 有/无 rerank，质量增益 vs 延迟代价 | M1 |
| E4 | 查询改写增益 | 无 / 多查询 / HyDE / 两者 | M1 |
| E5 | HNSW 参数 | m、ef_search 对召回-延迟的影响 | M2 |
| E6 | top-k 取值 | 召回 k、rerank 后 k | M2 |
| E7 | 拒答阈值 | τ 的 ROC 与 F1 最优点 | M1 |
| E8 | 全局摘要 | 有/无社区摘要在 `global` 类的表现 | Backlog |
| E9 | 生成档位降级 | main vs light 做生成 | M2（并入 [07](07-模型路由与成本.md)） |
