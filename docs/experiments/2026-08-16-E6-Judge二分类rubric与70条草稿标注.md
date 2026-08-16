# E6 · Judge 二分类 rubric 与 70 条草稿标注（2026-08-16）

## 结论先行

Judge 的 answer correctness rubric 从有序三档改为**二分类**（严格边界：部分正确记 0），
并在同一批 70 个 case 上完成助手草稿标注：**calibration 40/51（78.4%）、
validation 13/19（68.4%）、整体 53/70（75.7%）**。rubric 依据 calibration 修订一次
（v1→v2，只增补两条边界澄清，未改变任何已给标签），随后**冻结**，validation 19 条在冻结
之后才标。

**这批标签是助手草稿，尚未进入 `human-labels.csv`，reviewer 字段全空。** 在作者逐条复核
并署名之前，它们不构成人工标签，也不得用于运行 Judge 验收。

必须写在最前面的一条方法论警告：**如果作者只是通读一遍就批量接受这批草稿，后续算出来的
QWK 衡量的是"两个大模型是否互相同意"，不是"Judge 是否与人一致"。** 助手（Claude）与
Judge（DeepSeek heavy）虽是不同模型，但同读一份 rubric、同看一份 gold，其一致性天然高于
真实人机一致性，QWK 会被系统性高估。复核必须是实质性的逐条判断，尤其是下文标出的
7 条争议样本。

## 变更一：rubric 改为二分类

| 项 | 变更前 | 变更后 |
|---|---|---|
| `rubric_id` | `answer-correctness-3-level.v1` | `answer-correctness-binary.v2` |
| `label_scale` | `[0, 1, 2]` | `[0, 1]` |
| 档位语义 | 2 正确 / 1 部分正确 / 0 错误 | 1 正确 / 0 不正确（含部分正确） |

代码侧把写死的档数参数化（`LABELS` / `LABEL_COUNT`），混淆矩阵、边际分布与二次加权
kappa 的权重分母全部随之推导。

### 这个改动让门禁变严，而不是变松

**二分类下 QWK 恒等于无权重 Cohen's kappa**（权重矩阵 `((i-j)/(L-1))²` 在 L=2 时退化为
0/1）。三档时"人给 2、Judge 给 1"只按 1/4 权重计入分歧；二分类没有相邻档，任何分歧都是
满额分歧。因此沿用 `min_qwk = 0.85` 这个数值，实际验收标准比三档时**更严**。

这是一次预注册修改，必须记账：门槛数值未动，但其含义已变，不能把二分类下的 0.85 与三档
下的 0.85 当作同一个标准做纵向比较。已加测试 `test_binary_qwk_equals_cohen_kappa_
and_rejects_off_scale_labels`，用手算 Cohen's kappa 钉死该恒等式，并要求旧的 `2` 标签被
显式拒绝而不是静默落进统计。

### 严格边界，以及翻转成本为零的设计

采用严格边界：**部分正确记 0**。理由是产品承诺的是可溯源问答，一个方向对但缺关键事实的
答案不可交付；二分类若把部分正确归入"正确"，指标会掩盖恰恰最该暴露的失败。

但这个边界是可争的。为此草稿 CSV 除 `score` 外还写了一列 `severity`
（`correct` / `borderline` / `partial` / `wrong` / `refusal` / `correct_refusal`），
**严格与宽松边界的差别恰好只是 `partial` 这一档**。若改判宽松，把 3 条 `partial` 从 0 翻成
1 即可（53/70 → 56/70），无需重标任何一条。

## 变更二：rubric 冻结做成产物

新增 `judge_calibration freeze` 子命令，产出 `rubric-freeze.json`，并 fail-closed 两件事：

- 冻结时 calibration 必须已标完，否则"依据 calibration 修 rubric"无从谈起；
- 冻结时 validation 必须一条标签都没有，否则等于先看答案再定标尺。

`calibrate` 新增 `--rubric-freeze`，验收前校验 rubric 与 prompt 指纹自冻结以来未被改动，
漂移即拒绝出数，而不是照常给一个 QWK。

本轮冻结记录：

| 项 | 值 |
|---|---|
| `frozen_at` | 2026-08-16T06:21:31Z |
| `rubric_fingerprint` | `afc1a73cf55c1d9c…b221aff` |
| `prompt_fingerprint` | `dbc5fcd55443d4f2…9367ecbfaad` |
| 冻结时 calibration 已标 | 51 |
| 冻结时 validation 已标 | **0** |

v1→v2 的两条增补（均在标 calibration 时撞出，且都不改变已给标签）：

1. 概括或四舍五入本身不算与 gold 冲突；若概括值紧接着被正确数值限定，不因此记 0。
2. 清单类问题覆盖 gold 全部条目即记 1；额外补充的不冲突内容不加分也不减分，漏任一条目记 0。

validation 追加时回验了 calibration 标签摘要仍等于冻结记录里的 `label_digest`
（`e4fae80cb2776dbd…`），证明看过 validation 之后没有回头改 calibration。

## 标注结果

包目录：`eval/outputs/judge-calibration/m1-dev70-binary-20260816/`，
草稿文件 `assistant-draft-labels.csv`。

`example_set_fingerprint` 为 `7b2b9eef5a94…`，与改档前的三档包**逐字符一致**——题面、
答案与 51/19 拆分完全没动，变的只有 rubric 与 prompt 指纹。

| split | 正确 | 占比 |
|---|---:|---:|
| calibration | 40/51 | 78.4% |
| validation | 13/19 | 68.4% |
| 整体 | 53/70 | 75.7% |

分类别（正确数/总数）：

| 类别 | calibration | validation |
|---|---:|---:|
| global | 0/4 | 0/2 |
| multi_hop | 7/10 | 1/4 |
| single_hop | 12/14 | 5/5 |
| table | 8/9 | 2/3 |
| temporal | 3/4 | 2/2 |
| unanswerable | 10/10 | 3/3 |

`global` **0/6**，是全表最刺眼的一格；6 条全部因检索未召齐跨文档证据而拒答。归因见
[E7](2026-08-16-E7-retrieval-miss归因.md)。

### 17 条 0 分的构成

| severity | 条数 | 含义 |
|---|---:|---|
| `refusal` | 13 | answerable 却拒答 |
| `partial` | 3 | 答了但漏掉必要要点 |
| `wrong` | 1 | 事实答错 |

**13 条 `refusal` 与 E5 台账记录的残余 13 条误拒逐条吻合**（calibration 8 + validation 5），
且 13 条 `correct_refusal` 对应 13/13 的 unanswerable 正确拒答。已用脚本校验草稿标签与机械
可推的拒答状态**零冲突**——这不是巧合，是这批标签内部自洽的证据。

## 需要重点复核的 7 条

其余 63 条要么是逐字对得上的事实题，要么是明确拒答，复核成本很低。真正需要判断的是这些：

| example_id | 类别 | 我判 | 争点 |
|---|---|---:|---|
| `36a718da…` | multi_hop | 0 | CODESKILL 只答了相对最强 baseline 的 +4.01，漏掉相对 no-skill 的 +9.69。问题问的是 baselines（复数） |
| `f394e53e…` | table | 0 | 选型表 8 行漏"追踪 Langfuse"，且答案在中途被截断 |
| `44c05041…` | global | 0 | 只覆盖补遗一侧，TencentDB Agent Memory 那一处说法完全没出现 |
| `b44f4c74…` | multi_hop | 0 | 把 ReAct 的底座答成 GPT-3，gold 是 PaLM-540B。注意同一事实在 `ba27a830…` 上被答对并判 1 |
| `0be55027…` | single_hop | 1 | 三问都答到实质，但缺 E2M1 与 Blackwell 原生支持。若认为格式题必须点名 E2M1，应翻 0 |
| `39b99d4f…` | single_hop | 1 | 八股清单 8 项全覆盖，但混入 RAG vs 微调等非清单条目。若认为清单题多答即跑题，应翻 0 |
| `ad2a455a…` | single_hop | 1 | 四个吞吐数全对，但多写一句 gold 没有的"单流 100 tok/s"概括值 |
| `b08742ff…` | single_hop | 1 | 16,464 与 RapidAPI 都对，缺 gold 的"覆盖 49 个类别" |

（`b44f4c74` 与 `ba27a830` 这一对值得单独看：同一个底层事实，模型在两条题上给出相反答案。
这说明 0 分来自模型不稳定，而不是标注判据摇摆。）

## 一条标注缺陷，本轮不修

`648a70e9…`（english-dev，single_hop）的 gold 与问题比较对象不一致：题问"比 Mem0 便宜多少"，
gold 给的是"相对 full-context 少 30 倍 token"。E5 已记为标注缺陷。本条实际答案是拒答，
**无论 gold 如何修订都判 0**，因此本轮标签不受该缺陷影响，仍按 E5 决定放到下一数据版本修订
并完整重跑，不静默改写本轮基线。

## 决策与下一步

- 二分类 rubric 与冻结机制合入；`min_qwk` 数值不动，但在报告中必须标注其含义已变。
- **草稿不入 `human-labels.csv`。** 作者逐条复核后，由作者署名填写
  `score/reason/reviewer/reviewed_at`，重点是上表 7 条争议样本。
- 复核完成前不运行 DeepSeek heavy Judge：Judge 包仍为 `model_send_authorized=false`，
  端点健康可用不等于已获准发送 70 条项目数据。
- 报告 QWK 时必须同时披露人工标签的来源方式（助手起草 / 作者逐条确认），
  否则 QWK 会被读成人机一致性。
- `agent_task` 仍为 0，七类验收继续阻塞，与 J0 台账记录一致。

## 追溯

- 上游：[E4 · 70 条 dev 扩展评测收口](2026-08-16-E4-70条dev扩展评测收口.md)、
  [E5 · Evidence gate 误拒归因与修复](2026-08-16-E5-evidence-gate误拒修复.md)
- 并行：[E7 · 11 条 retrieval miss 归因](2026-08-16-E7-retrieval-miss归因.md)
- 数据纪律：[J0 · Judge 校准与 120 条扩集预检](2026-08-16-J0-Judge校准与120条扩集预检.md)
