# E5 · Evidence gate 误拒归因与修复（2026-08-16）

## 结论先行

在同一份 70 条 human dev、同一 heading 检索结果和同一 Qwen 生成端点上，evidence gate
修复后的拒答决策正确率从 **49/70（70.0%）提升到 57/70（81.4%）**；57 条 answerable
实际回答从 **36 条提升到 44 条**，13 条 unanswerable 仍保持 **13/13 正确拒答**。配对变化为
10 条误拒恢复、2 条原本回答的题退化为拒答，净恢复 8 条。

这次修复不是放宽“证据不足也回答”的阈值，而是修正 gate 看见什么证据：旧实现会在 rerank
后的候选间轮询 block，6000 字符耗尽前经常只取到每个候选的第一段，破坏了 rerank 的优先级。
改为按最终排序连续打包后，门控输入保留高优先级 chunk 的完整上下文；同时对非法 JSON/schema
响应补一次同问题修复重试，第二次仍非法则继续 fail-closed。

剩余 13 条中，**11 条是检索未完整召回 gold，1 条是 12k answer 证据预算仍缺第二跳，1 条是
SimpleMem 题目与 gold 的比较对象不一致**。最后一条不应解释为 gate 模型误判：题目问“比 Mem0
便宜多少”，gold 却给出“比 full-context 少 30 倍 token”。本轮冻结结果保留原样并记为标注缺陷；
修订题目必须进入下一数据版本并重跑，不静默改写本轮 70 条基线。

## 归因方法与修复前结果

归因脚本用正式 retrieval report、generation report 和数据库中的 chunk/block 重建 gate 输入，
逐 gold span 检查三个层次：是否被 Top-K 召回、是否进入 gate 证据、是否进入 12k answer 证据。

修复前 21 条误拒：

| 原因 | 条数 | 含义 |
|---|---:|---|
| retrieval miss | 10 | 至少一个 gold span 未被正式检索命中 |
| gate packing miss | 9 | 检索已命中，但旧 round-robin gate 打包没有把完整 span 送给模型 |
| gate model false negative | 1 | gold 完整可见但模型仍判不足；后续确认是 SimpleMem 标注缺陷 |
| invalid response | 1 | gate 返回非法结构，按 fail-closed 拒答 |

对全部 57 条 answerable 做反事实重建：旧 round-robin 打包完整覆盖 gold 的为 24 条，按 rerank
顺序连续打包为 38 条，改善 14 条、退化 0 条。这是选择顺序打包的直接依据。

修复前报告：
`eval/outputs/evidence-gate-analysis/m1-dev70-heading-20260816/report.json`。

## 代码变更

- `grounded_answer.build_gate_evidence` 按 rerank 最终顺序连续打包，删除旧的逐 segment 1200
  字符轮询配置；gate 总预算仍为 rerank 6000 / 非 rerank 3000 字符。
- `assess_evidence_sufficiency` 对非法 JSON/schema 只修复重试一次；重试仍失败继续拒答，不冒答。
- generation report 新增 top/second score、margin、low-margin、rerank 状态与 gate
  sufficient/reason/model/provider，后续误拒不再依靠日志猜测。
- 新增 `eval/evidence_gate_analysis.py`，把检索缺失、gate 打包缺失、answer 证据预算缺失和
  gate 模型判断分开。

## 受控复跑

| 指标 | 修复前 | 修复后 | 变化 |
|---|---:|---:|---:|
| 完成 / error | 70 / 0 | 70 / 0 | 不变 |
| 拒答决策正确 | 49/70（70.0%） | 57/70（81.4%） | +8 条 / +11.4pp |
| answerable 实际回答 | 36/57（63.2%） | 44/57（77.2%） | +8 条 / +14.0pp |
| unanswerable 正确拒答 | 13/13 | 13/13 | 不变 |
| constraint pass | 31/70 | 40/70 | +9 条 |
| 非拒答 citation validity | 36/36 | 44/44 | 不变（100%） |
| citation-gold alignment 代理 | 39/139 | 48/155 | +9 个对齐引用 |
| 平均延迟 | 6169.5ms | 6411.7ms | +242.2ms |
| token 总计 | 881,283 | 1,057,713 | +176,430 |

这里的 latency/token 上升主要来自多回答了 8 条，不能直接解释为 gate 自身开销。自动
`citation-gold alignment` 仍只是字符覆盖代理；answer correctness 要等人工标签和校准 Judge。

类别上的拒答正确数：global 2/6 → 1/6，multi-hop 9/14 → 10/14，single-hop 13/19 →
17/19，table 7/12 → 11/12，temporal 5/6 和 unanswerable 13/13 不变。两条回归均已进入
人工 Judge 包，不能只报告净收益而隐藏回归。

修复后 generation manifest：
`eval/outputs/dev-suite-generation/20260816T025245.625013Z-m1-dev70-heading-gatefix-20260816/manifest.json`。

最终归因报告：
`eval/outputs/evidence-gate-analysis/m1-dev70-heading-gatefix-final-20260816/report.json`。

## Judge 与 heavy 端点准备

修复后 70 个唯一 case 已导出到
`eval/outputs/judge-calibration/m1-dev70-six-class-gatefix-20260816/`：

- calibration 51 / validation 19，两个 split 均覆盖六类；
- `human-labels.csv` 已按 calibration 优先排序，0/1/2、理由、复核人和时间保持空白，等待真实人工填写；
- `human-review-guide.md` 固化评分标准与 calibration/validation 隔离纪律；
- DeepSeek heavy 已定位并通过 exact-model 与合成 chat smoke，实际模型为
  `deepseek-v4-flash`；健康报告明确记录“未发送项目数据”；
- Judge 包仍为 `model_send_authorized=false`。端点健康可用不等于已获准发送 70 条项目数据，
  人工标签和该数据范围授权齐备前不得执行正式 Judge run。

## 决策与下一步

- 合入 gate 顺序打包和一次结构修复重试；不放宽 unanswerable 安全边界。
- 下一轮检索 badcase 聚焦剩余 11 条 retrieval miss，按 global / multi-hop 优先，不继续调 gate。
- 单独验证 AutoGen + CODESKILL 的证据预算/查询分解方案，避免为 1 条样本全局盲目扩大上下文。
- 下一数据版本修正 SimpleMem 问题比较对象并完整重跑；本轮数字不追溯篡改。
- 人工先填 calibration 51 条；rubric 冻结后再填 validation 19 条，之后才运行 DeepSeek heavy Judge。
- 10 条 test 继续隔离，最终方案冻结前不访问。

后续见 [E6 · Judge 二分类 rubric 与 70 条草稿标注](2026-08-16-E6-Judge二分类rubric与70条草稿标注.md)
（rubric 改二分类、草稿标注与冻结）与
[E7 · 11 条 retrieval miss 归因](2026-08-16-E7-retrieval-miss归因.md)
（11 条拆成截断损失 6 条与候选缺失 5 条，结论是不该调 `top_k`）。
