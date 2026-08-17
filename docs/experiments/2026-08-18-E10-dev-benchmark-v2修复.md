# E10 · dev benchmark-v2 事实组与时序修复

## 结论

`m1-dev-70` 的结构轴原本有效，但 flat `gold_spans` 会漏标等价证据，temporal_ctx 也未进入
检索链路。修复后 suite 仍为 70 human dev、0 test 访问；事实组和 temporal 版本过滤正式生效。
旧报告与 benchmark-v2 指纹不同，不允许直接混比。

## 修复内容

- 数据库新增 `gold_evidence_groups=[{fact_id, alternatives[]}]`；旧数据机械迁为一 span 一事实组。
- 5 个已人工确认的等价证据加入 4 条样本；每个事实命中任一 alternative 即覆盖。
- 一条跨两个 parsed block 的 global span 拆成两个独立必需事实，避免 θ=0.5 假阳性。
- 一条全部 gold 落在同一 heading chunk 的 multi-hop 改为 single-hop；类别变为
  single-hop 20 / multi-hop 13，其余不变。
- 4 条 raw-quote gold answer 改写为综合答案，3 条英文 5-gram 泄漏问题重新措辞。
- 6 条 temporal 和 1 条时效型 unanswerable 的 temporal_ctx 统一为冻结的资料库快照时点
  `2026-08-14T12:00:00Z`；全部 temporal gold 在该时点可见。
- dense/lexical/coverage/grounded-answer/eval 全链路透传 temporal_ctx。历史查询绕过只覆盖当前
  `is_searchable=true` 的 HNSW，按版本有效期精确扫描。

## 可恢复性与审计

- migration 前快照：`eval/outputs/dev-benchmark-repair/pre-schema-d9eece144ab3.json`
- SHA256：`d9eece144ab3fe8078699a866c699f07e679e8c74aea49973b221b7603acd8e3`
- 修复 runner：`eval/repair_dev_benchmark_v2.py`，只接受四个 dev dataset；写前快照、事务更新、
  postflight，且显式检查 temporal gold 可见性。
- 最终 postflight：70 items、88 canonical spans、5 equivalent alternatives、0 stale span、
  0 invalid group、0 raw-quote answer、0 gold constraint failure、0 temporal invisible gold。
- `m1-test-10` 未加载、未运行、未用于修订；Alembic 只对全表做机械 schema/backfill。

## 新基线

- 检索 manifest：
  `eval/outputs/dev-suite-retrieval/20260817T171842.627194Z-benchmark-v2-rrf-top5-20260818/manifest.json`
- 链路：heading，dense + ts_rank → RRF，Top-5，diagnostic-50，4000 字符预算。
- suite fingerprint：`f04618a7628e2d92e17a7cee7579db3339931abd94a905a1df7f9da919ff3030`
- suite definition SHA256：`31feb602f33a75b225361f13557073355d1841180f2450dec00b1baa4319c037`
- `eval/snapshots/retrieval.json` 已从该报告重新生成（70 条），随后用 `--against working`
  实跑门禁，全部规则通过；generation snapshot 未修改。

| 指标 | benchmark-v2 |
|---|---:|
| 事实组 Recall@5（字段兼容名 `span_recall_at_k`） | 0.7047 |
| 4000 字符预算 Recall | 0.7822 |
| nDCG@5 | 0.5726 |
| MRR | 0.5889 |
| temporal Recall@5 / budget Recall | 0.7500 / 0.8333 |

## 决策边界

该修复提高的是 benchmark 的判分正确性，不代表检索系统本身突然变强。所有旧 baseline、P1
诊断和门禁快照都属于 flat-span 口径；引用旧数字时必须标明 `benchmark-v1`，不能与 v2 做
无说明的纵向提升。正式门禁应以本次新指纹重新 snapshot。
