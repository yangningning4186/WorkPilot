# P1-L · 受约束 Agentic RAG 严格配对实验

> 状态：真实 dev70 已完成；Agentic navigation 保住拒答安全，但目标恢复仅 1/14、
> 与更简单的文档二跳相同，且延迟显著恶化，因此不进入生产。
> 本实验只读资料库，不访问隔离 test，不修改生产检索开关。

## 1. 要回答的问题

P1-K 已证明“真实子查询 + coverage-aware Top-5”在 14 条复杂误拒题上恢复 0/14，
其中 32 个 gold spans 有 11 个在所有真实子查询 Top-50 中均不可达。P1-D 同时给出
文档 oracle 二跳 4/6 的上限信号。P1-L 验证：加入文档/实体发现、文档内章节导航，
并允许一次只针对 evidence gate 缺失项的补查，能否把 oracle 信号转成非 oracle 收益。

它不是通用 Agent 实验。没有自由工具选择、无限 reflection 或写操作；状态机最多执行：

```text
规划子事实
  → 按子事实发现文档
  → 文档内局部 dense
  → 章节同路径 + 相邻 chunk 扩展
  → 每个子事实独立 CE 排序并写入证据台账
  → evidence gate
       ├─ sufficient → 结束
       └─ missing_aspects → 只补查缺失项一次 → 再 gate → 结束
```

## 2. 冻结轴与四个严格配对变体

同一次 runner 读取 `m1-dev-70`，共享逐题的原问题 dense/lexical 候选，并按同一
`item_id` 输出四个变体：

| 变体 | 最终口径 | 作用 |
|---|---|---|
| `rrf_top5` | 原问题 dense + ts_rank → RRF Top-5 | 生产基线 |
| `p1k_coverage` | 同一规划的真实子事实各自检索 → coverage Top-5 | 复现 P1-K 机制 |
| `doc_m3_local_n10` | 原问题文档 Top-3 × 局部 dense Top-10 → CE → Top-5 | P1-F M3×N10 的 Top-5 对照 |
| `agentic_navigation` | 子事实文档路由 → 章节导航 → 子事实 CE → 证据台账 Top-5 → 最多一次补查 | 候选方案 |

三个互斥评测轴固定为：

- **主恢复轴**：从 P1-I attribution report 提取与 P1-K 相同的 14 条
  `retrieval_miss AND category IN ('multi_hop','global')`；启动时不是 14 条直接拒绝。
- **回退轴**：完整 57 条 answerable dev，报告完整 gold 证据回退与 baseline 已放行题回退。
- **安全轴**：完整 13 条 unanswerable dev，报告 evidence gate 误放；不是 13 条直接拒绝。

这里的 `gate_sufficient` 只是拒答决策代理，不冒充最终 answer correctness。若本实验过第一门，
再对候选方案运行生成与校准 Judge，避免一开始就把检索、门控和生成三种变化混在一起。

## 3. 预算与可归因性

- 规划最多 4 个子事实；规划失败逐位回退原 RRF。
- 首轮最多选择 3 篇文档；每个子事实最多导航其中 2 篇。
- 每次局部导航取 2 个章节 seed，加入同章节和前后各 1 个 chunk，单路最多 10 个候选。
- Agent 候选并集最多 80；最终统一 Top-5。
- evidence gate 只有在明确返回 `missing_aspects` 时才允许补查，且最多一次。
- 输出分别记录业务阶段 `logical_model_calls`、本地 `reranker_calls`、局部检索次数与端到端延迟；
  四个主指标和延迟均按同一条 70-item 轴做 10,000 次 paired bootstrap，无关样本记为 ineligible。
  网关内部 schema repair/升档重试不混进逻辑调用数，需用 `llm_calls` 审计核对。
- 每题完成后写 checkpoint；suite、attribution、关键配置任一指纹变化均禁止复用。
- 单题遇到 `httpx.TransportError` 最多重试 2 次（共 3 次 attempt），次数写入报告；
  不吞 schema、数据轴或检索契约错误。

## 4. 预注册判定

先看安全，再看收益：

1. 13 条 unanswerable 必须继续 13/13 拒答；出现误放，不进入生产讨论。
2. `gate_invalid` 必须为 0；否则本轮不能形成质量结论。
3. 在 57 条 answerable 上单列完整证据回退和 gate 回退，不能只报 14 条目标恢复。
4. 14 条目标轴报告完整证据恢复数；若仍为 0，则停止调循环次数，回到文档索引与 chunk
   可检索性，不把“更长的 Agent 循环”当下一步。
5. 即使点估计改善，只要代价与回退不明确可接受，也不修改生产默认；完整 dev 的配对
   bootstrap 与生成/Judge 是后续上线门，不由本 runner 越权下结论。

## 5. 运行方式

运行会向当前配置的规划/门控模型发送问题文本和截断证据，必须在参数中如实记录授权；
报告与 checkpoint 写入 Git 忽略目录：

```bash
PYTHONPATH=backend:. UV_CACHE_DIR=/private/tmp/workpilot-uv-cache \
uv run --project backend python -m eval.p1_agentic_rag_experiment \
  --suite eval/suites/m1-dev-70.json \
  --attribution-report \
    eval/outputs/p1-evidence-gate-attribution/20260817-P1-I-pool50-top5-gate3000-sequential-v2/report.json \
  --label P1-L-agentic-rag-dev70-20260818 \
  --authorization-note '<授权人、日期、允许发送的问题/证据范围>' \
  --reranker-base-url http://127.0.0.1:8012 \
  --gate-max-chars 6000
```

实现：`eval/agentic_retrieval.py`、`eval/p1_agentic_rag_experiment.py`。
PR 层回归：`backend/tests/test_p1_agentic_rag_experiment.py`。

## 6. 正式结果

- 机器报告：`eval/outputs/p1-agentic-rag/20260817T163223Z-P1-L-agentic-rag-dev70-20260818.json`
- 人工报告：`eval/outputs/p1-agentic-rag/20260817T163223Z-P1-L-agentic-rag-dev70-20260818.md`
- 输入指纹：`2311b85464c3aae9b2e29a5940284d7022469348bd07b1be153072c7feb3f498`
- reranker：`BAAI/bge-reranker-v2-m3`，MPS/float16，batch 4，max_length 512。
- 规划器实际分解 52/70；目标 14 条中分解 13 条。按类别为 global 6/6、multi-hop
  13/14、single-hop 12/19、table 10/12、temporal 6/6、unanswerable 5/13。

| variant | 目标完整 | answerable 完整 | 完整回退 | gate 放行 | gate 回退 | 安全拒答 | mean / p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| RRF Top-5 | 0/14 | 34/57 | 0 | 37/57 | 0 | 13/13 | 1.19s / 1.50s |
| P1-K coverage | 0/14 | 33/57 | 1 | 40/57 | 0 | 13/13 | 2.75s / 3.98s |
| M3×N10 → CE → Top-5 | 1/14 | 35/57 | 2 | 38/57 | 4 | **12/13** | 3.75s / 4.80s |
| Agentic navigation | **1/14** | **36/57** | 2 | **40/57** | 2 | **13/13** | **9.14s / 17.35s** |

配对 bootstrap 相对 RRF Top-5：

- Agentic 目标完整率 Δ=+0.071，95% CI `[0,+0.250]`，跨 0；只有 1 条恢复。
- answerable 完整率 Δ=+0.035，95% CI `[-0.051,+0.123]`，4 胜 / 2 负，跨 0。
- gate 放行率 Δ=+0.053，95% CI `[-0.036,+0.143]`，5 胜 / 2 负，跨 0。
- 安全拒答严格持平 13/13；但这是 13 条小样本安全检查，不外推总体误放率。
- mean latency Δ=+7.949s，95% CI `[+6.668,+9.216]s`，显著恶化；平均逻辑模型调用
  4.31 次、CE 调用 2.99 次，而 RRF 基线为 2.00 / 0 次。

## 7. 补查循环与失败归因

20/70 条触发唯一一次 `missing_aspects` 补查，只有 **1/20** 让 gate 从不足翻为充分，
其余 19 条仍不足。这一条翻转不在 P1-K 14 条目标轴，且其 exact gold coverage 已经完整；
说明循环主要是在重复确认已有证据，不是在恢复目标 miss。

Agentic 的目标轴唯一恢复与 M3×N10 是同一条题；换句话说，真正兑现收益的是文档局部检索，
不是 Agent 循环。其余 13 道目标轴题的 exact gold 仍不完整。另有 4 道目标轴题被 gate 放行但 gold
不完整，可能是窗口内等价证据，也可能是门控假阳性；在人工引用复核前不能记为“恢复”。

运行中在第 21 条的 evidence gate 遇到一次 `RemoteProtocolError`。前 20 条 checkpoint 保留；
随后 runner 增加仅捕获 `httpx.TransportError` 的三次有限 attempt 并续跑。该改动只影响传输
恢复，不改变候选、门控或评分逻辑。最终 70 条全部完成、0 gate invalid；正式报告中的
`transport_retry_count=0` 指完成行内的重试次数，不包含这次进程级中断，故在此单独披露。

## 8. 决策

**P1-L 不通过，不接生产，不增加循环次数。** 理由不是 Agentic RAG 完全无效，而是当前收益
已经被更简单的文档二跳覆盖，额外 planner、按子事实 CE 和缺失项循环没有兑现目标恢复，
却把 p95 从 1.50s 推到 17.35s。

下一步回到“正确文档可达、正确 chunk 不可达”的表示层问题：优先建设文档级摘要/实体索引，
再对固定 14 条轴验证文档路由和文档内 lexical/section 检索。只有这个确定性二跳显著超过
M3×N10 后，才重新评估是否需要 Agent 控制器；当前不再调 planner prompt、循环次数或 Top-K。

## 9. benchmark-v2 复跑：v1 的 exact-gold 结论被修正

后续 benchmark 审计确认，P1-L v1 中 4 条“gate 充分但 exact gold 不完整”的样本都有直接、
等价的替代证据。v1 的 flat `gold_spans` 把有效命中记成 false negative，因此“Agentic 与
M3×N10 同为 1/14、没有增量”的结论作废。benchmark-v2 引入“事实组 × 等价证据”，并修复
temporal_ctx、跨 block span、伪 multi-hop、raw-quote answer 和问题泄漏后重跑。

- 新报告：`eval/outputs/p1-agentic-rag/20260817T173623Z-P1-L-agentic-rag-benchmark-v2-dev70-20260818.json`
- 输入指纹：`7f53ebea3001d0bf9b40e2601c9da8174e507f50c8841f3165f12e3338f7de52`
- 70/70 完成，1 次 transport retry 自动恢复，0 gate invalid，test 访问为 0。

| variant | v1 目标完整 | v2 目标完整 | v2 answerable 完整 | v2 安全拒答 | v2 mean / p95 |
|---|---:|---:|---:|---:|---:|
| RRF Top-5 | 0/14 | 2/14 | 36/57 | 13/13 | 1.12s / 1.42s |
| P1-K coverage | 0/14 | 2/14 | 35/57 | 13/13 | 2.66s / 3.84s |
| M3×N10 → CE | 1/14 | 2/14 | 37/57 | **12/13** | 3.59s / 4.26s |
| Agentic navigation | 1/14 | **5/14** | **40/57** | **13/13** | **8.99s / 17.47s** |

Agentic 相对 RRF 的目标完整率 Δ=+0.214，95% CI `[0,+0.455]`；answerable 完整率
Δ=+0.070，95% CI `[-0.019,+0.167]`，均因 n 小而跨 0。它现在有明确的点估计增益：
目标轴净恢复 3 条，完整 answerable 从 36 提升到 40（6 胜 / 2 负），且保持 13/13 安全；
但 mean 增加 7.87s、p95 达 17.47s，延迟显著恶化。

**修正后的决策**：Agentic RAG 不是“机制无增益”，而是“质量方向成立、统计与 SLA 尚未过门”。
仍不全局上线；下一步应先做条件路由和更便宜的文档内排序，把 52/70 的过度分解与约 3 次 CE
调用降下来，再扩充独立 multi-hop validation。不能再引用 v1 的 1/14 作为反对 Agentic 的证据。
