# E6 · Judge 二分类 rubric 与 70 条草稿标注（2026-08-16）

## 结论先行

Judge 的 answer correctness rubric 从有序三档改为**二分类**（严格边界：部分正确记 0），
并在同一批 70 个 case 上完成助手草稿标注：**calibration 40/51（78.4%）、
validation 13/19（68.4%）、整体 53/70（75.7%）**。rubric 依据 calibration 修订一次
（v1→v2，只增补两条边界澄清，未改变任何已给标签），随后**冻结**，validation 19 条在冻结
之后才标。

**作者已于同日完成复核并署名，标签已落 `human-labels.csv`**（详见下文"复核完成"）。
8 条争议样本由作者显式逐条裁定，结论与草稿逐条一致；其余 62 条为作者确认草稿。

Judge 跑批已完成（70/70），人工与 Judge **打分逐条相同，QWK=1.0，门禁 `status=failed`**——
失败项是退化重采样导致的 `qwk_bootstrap_incomplete`，与一致性无关。但这个 1.0 的信息量很低：
validation 19 条里只有 2 条真正需要判断力，其余是拒答类与逐字事实题，明显样本上一致属预期。
**作者决定 Judge 层本轮到此为止，不宣布校准通过，也不据此出质量结论。**

一条必须跟着数字走的说明：**标签由助手起草、作者确认，Judge 是另一个大模型读同一份
rubric 判同一批答案。** 报告任何 QWK 时必须同时披露标签来源方式与区分度拆解，
`human-labels-provenance.json` 已把前者写成产物。

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

## 复核完成（2026-08-16）

作者复核后填入 `human-labels.csv`，`reviewer = xingzhi <ningzhi.yang@vim-technology.com>`，
`reviewed_at = 2026-08-16T06:51:27Z`。填写只动 `score/reason/reviewer/reviewed_at`
四列，fingerprint 与冻结内容逐字保留；`load_human_labels` 回验 70/70 通过
（无内容漂移、归因字段完整、无空 reason）。

**分数一条未变，53/70 与草稿完全相同。** 下表 8 条争议样本由作者显式逐条裁定，
理由用作者自己的措辞（不是助手草稿的措辞）；其余 62 条为作者确认草稿理由。

| 来源方式 | 条数 |
|---|---:|
| 作者显式逐条裁定 | 8 |
| 作者确认助手草稿 | 62 |

这个区分写进了 `human-labels-provenance.json`
（`review_mode = "assistant-drafted, author-adjudicated"`）。**不要把它表述成
"70 条全部由人独立标注"**——8 条是独立裁定，62 条是确认，二者对 QWK 的证据强度不同。

8 条裁定结论与草稿逐条一致，这是判据本身站得住的正面证据，但它同时意味着
上文的高估风险仍然存在，没有被这次复核消除。

## Judge 跑批与验收（2026-08-16）

作者授权后向自部署内网端点 `172.16.1.13:8002`（`deepseek-v4-flash`）发送 70 条
`question` / `gold_answer` / `answer`（不含 citations 与文档标识，不访问隔离 test），
70/70 完成、`repair_retries=0`、模型身份单一。

### 结果：完全一致，但这批样本对 Judge 几乎没有区分度

| 指标 | calibration (51) | validation (19) | 整体 (70) |
|---|---:|---:|---:|
| accuracy | 1.0000 | 1.0000 | 1.0000 |
| QWK（=Cohen's kappa） | 1.0 | 1.0 | 1.0 |
| 分歧条数 | 0 | 0 | **0** |

已独立核对这不是接线错误：Judge 侧 17×0 / 53×1，与人工侧逐条相同；70 条理由去重后 61 条
（非复制粘贴），输出 token 14,142，模型身份唯一，授权指纹唯一。Judge 确实独立跑了一遍。

按判断难度拆开这 70 条，就知道这个 1.0 该怎么读：

| | calibration (51) | validation (19) |
|---|---:|---:|
| 机械可判（拒答类；`answerable` 直接写在 prompt 里给了 Judge） | 18 | 8 |
| 逐字事实题（数字 / 命令 / URL 对上即可） | 28 | 9 |
| **真正需要判断力**（`borderline` / `partial`） | 5 | **2** |

验收只看 validation：**19 条里只有 2 条真正考验 Judge**，其余 17 条是送分题。
在明显的题上完全一致本来就是应该的，反过来才该担心。所以这个 1.0 既不能说明 Judge 可靠，
也不构成 Judge 不可靠的证据——**它主要说明这批样本没有区分度**。

还有一层需要记下来。4 条 `borderline` 上 Judge 给出的判据是：

- `ad2a455a`：「100 tok/s 随后被具体数值限定，不构成冲突」
- `39b99d4f`：「额外补充的内容与参考答案不冲突，不影响评分」
- `0be55027`：「未提及 E2M1 和 Blackwell 属于非必要附带细节，不扣分」

这三句正是 rubric 里的边界条款，其中前两条是助手在标 calibration 时撞出歧义后写进 v2 的。
链条是"助手判这几条 → 把判据写成 rubric → Judge 读 rubric → 判这几条 → 一致"。
条款只取自 calibration 样本，协议未被违反；但 calibration/validation 拆分能挡住
"拿 validation 样本调参"，挡不住"把判断编码进标尺"。评估 rubric 自身的泛化性时要记得这一点。

因此本轮**不宣布 Judge 校准通过**，也不据此开始用 Judge 出质量结论；但也不把它记为失败。

### 门禁状态：failed（原因与一致性无关）

`status = failed`，唯一失败项是 `qwk_bootstrap_incomplete`：validation 19 条中标签分布为
6 个 0 / 13 个 1，10000 次配对重采样里有 **8 次**抽到全部同一类，此时两侧边际都是常量、
chance agreement 分母为 0，kappa 无定义被丢弃，`effective_resamples=9992 < 10000` 即判失败。
已用同分布模拟复现（约 9 次），确认是退化重采样的必然产物。

这不是"一致性不达标"。但**不建议为此放宽门禁**：它实际上在说"这个样本量与标签分布撑不起
稳健的 kappa 估计"，这个提醒是对的。真正的阻塞点在上一节，不在这个阈值。

`--rubric-freeze` 校验通过，rubric 与 prompt 指纹自 06:21:31Z 冻结以来未被改动。

产物：`eval/outputs/judge-calibration/m1-dev70-binary-20260816/`
（`judge-predictions.jsonl`、`report/report.json`、`report/report.md`）。

### 跑批中暴露并修复的网关 bug

`OpenAICompatibleProvider.complete` 原实现为
`text = str(payload["choices"][0]["message"]["content"])`。reasoning 模型在推理耗尽
`max_tokens` 时返回 `content=null`，`str(None)` 得到字符串 `"None"` 而不抛异常——
把"模型没给内容"静默伪装成内容，一路传到下游才在 JSON 解析处炸，报错指向错误的地方。

按约束 1 所有 LLM 调用都经这个网关，因此该缺陷影响 evidence gate、生成轨与拒答判定，
不只 Judge。已改为显式抛 `ProviderResponseError` 并带 `finish_reason`。

同时：`JUDGE_MAX_TOKENS` 从 500 提到 2048（实测长样本上 500 会被推理 token 耗尽；该值不进
`prompt_fingerprint`，发给模型的文本未变，故不违反冻结）；按 E5 对 evidence gate 的同一政策
补一次重试后仍非法则 fail-closed；解析失败的报错补上 `example_id` / 类别 / split /
已完成条数 / 原始输出。首次跑批即因该缺陷整批失败，重跑后 `repair_retries=0`。

## 需要重点复核的 7 条（复核前）

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
- **Judge 层本轮到此为止**（作者 2026-08-16 决定）。跑批闭环已经打通、产物齐全，
  在明显样本上完全一致属预期行为，不再为此追加工作。
- 但**不宣布校准通过**：`status=failed` 且 validation 只有 2 条有区分度，
  现阶段不用 Judge 出质量结论。恢复推进时的入口是补有区分度的样本
  （"答了但有细节问题"这一类），而不是调门禁阈值。
- 不为 `qwk_bootstrap_incomplete` 放宽门禁。它反映的是样本量与标签分布撑不起稳健
  kappa 估计，这个提醒成立；样本量上去后自然消解。
- 报告任何 QWK 时必须同时披露标签来源方式（8 条显式裁定 / 62 条确认草稿 / 0 条盲标）
  与上表的区分度拆解，不能只报 1.0。
- 网关 `content=null` 缺陷已修。已回查 E4/E5：扫描 140 条生成结果，`answer`
  恰为字符串 `"None"` 的为 **0 条**，E4/E5 的数字未受该缺陷污染，不需要重跑。
- `agent_task` 仍为 0，七类验收继续阻塞，与 J0 台账记录一致。

## 追溯

- 上游：[E4 · 70 条 dev 扩展评测收口](2026-08-16-E4-70条dev扩展评测收口.md)、
  [E5 · Evidence gate 误拒归因与修复](2026-08-16-E5-evidence-gate误拒修复.md)
- 并行：[E7 · 11 条 retrieval miss 归因](2026-08-16-E7-retrieval-miss归因.md)
- 数据纪律：[J0 · Judge 校准与 120 条扩集预检](2026-08-16-J0-Judge校准与120条扩集预检.md)
