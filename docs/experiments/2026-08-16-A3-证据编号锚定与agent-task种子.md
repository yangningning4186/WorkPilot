# A3｜证据编号锚定与首个 agent_task 种子

**日期**：2026-08-16

**模型**：`qwen3.6-35b-a3b`（统一 `ModelGateway`，temperature 0）

**数据边界**：20 篇真实已激活文档；未访问 10 条隔离 test

## 结论

把 `evidence_quotes` 从“要求模型逐字抄写”改成“模型选择原文 E 编号、服务端按字符区间
回填原文”，解决了校验器与生成模型行为错配：首轮成功率从 A2 的 **30%（6/20）** 提升到
**75%（15/20）**；与线上一致的两轮面向模型修复后为 **100%（20/20）**。20 个成功
卡片全部使用编号协议，所有返回 quote 都是模型可见正文的精确切片。

这不是放松 evidence gate。兼容的旧 quote 路径仍只允许 exact 或可逆版式归一化定位；
语义改写、标点变化和歧义归一化匹配继续 fail closed，不使用 fuzzy/embedding 阈值。

## 协议与失败边界

- 正文被切成最多 400 字符的原文项，编号由 `char_start` 稳定生成，例如 `E00254`。
- 模型只输出 1–8 个 `evidence_refs`；服务端校验编号存在且不重复，再回填原文切片。
- `ReviewCard` 对下游仍暴露既有 `evidence_quotes`，不把迁移成本扩散到图状态和前端。
- 旧 checkpoint / A1、A2 回放仍可提交 `evidence_quotes`；只忽略空白、软连字符与 NFKC
  兼容形式，并且归一化匹配必须唯一。
- 编号不存在、重复、超过 8 个、JSON 不完整或语义改写均拒绝。

## 真实结果

产物：

- `eval/outputs/agent-evidence-anchor/a3-20260816/`
- `eval/outputs/agent-evidence-anchor/a3-repair-20260816/`

| 指标 | 首轮 | 线上同构（最多两轮修复） |
|---|---:|---:|
| 成功 | 15/20 | 20/20 |
| 成功率 | 75% | 100% |
| refs 协议成功 | 15 | 20 |
| 旧 quote 协议成功 | 0 | 0 |
| 返回值均为原文切片 | 是 | 是 |

首轮剩余 5 个失败中，4 个是 `evidence_refs` 超过 8 条，1 个是 JSON 截断；后者第二轮
变成数量超限，第三轮恢复。因此这批数据里已没有 `quote_not_verbatim`，修复轮也没有
靠静默截断数组获得通过。

## agent_task 已启动但未冒充正式标签

质量门槛通过后，`eval/agent_task_seed_runner.py` 用 AgentBench、Agent-World 与 GAIA 三篇
真实文档执行完整固定图，经过 HITL 批准后写入隔离评测目录。源 run：
`01a00a26-db6c-7d0b-80c7-d2da6c76a391`，实际模型调用 5 次、41,943 token；六步
attempt 全部成功，`write_note` 幂等调用成功且 retry 0，产物 SHA-256 为
`c9d3a8d656ad3836c68c4492998953ad4bfdc5d6c125f9c71d720fe1825dd6e6`。

种子包位于 `eval/outputs/agent-task-seeds/seed-20260816/`，包含真实 Markdown 产物、六步
`gold_tools` 非空参数、run/幂等/哈希证据。目前状态是 `pending_human_review`，只有 1 条，
不会直接混入 70 条六类基线，也不声明七类 Judge 已完成。

`eval/agent_task_rules.py` 已建立独立规则轨，当前种子九项全过：工具序列、必要参数、工具
状态、workflow_type、终态、HITL 决策、产物路径边界、SHA-256 和内容约束。篡改产物、
路径逃逸、工具乱序与 HITL 漂移均有 fail-closed 单测。

## 下一步

1. 人工复核种子 001 的目标措辞、工具参数子集和产物内容；
2. 增加拒绝写入、重复批准/恢复、预算熔断等任务变体，并为不同预期终态定义规则轨；
3. 冻结至少一批经人工批准的 `agent_task` 后，再移除 Judge 校准中的七类阻塞门禁。
