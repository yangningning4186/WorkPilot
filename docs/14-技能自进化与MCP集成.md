# 14 · 技能自进化与 MCP 集成

> **实现状态（2026-08-28）**：MCP client 与 Skill 的本地生命周期、候选蒸馏和人工晋升已经实现。
> MCP server 尚未实现；基于自动评测回放的 Skill 晋升门禁、持续效果统计与自动退化仍是后续工作。

---

## 0. 定位与当前边界

Skill 补的是程序性记忆：长期记忆回答“用户和世界是什么样”，Skill 回答“这类任务怎样做”。
MCP client 则把外部工具接进 Cowork，同时把目录漂移、参数校验、权限审批和副作用重放风险纳入统一治理。

当前边界如下：

| 能力 | 2026-08-28 状态 |
|---|---|
| MCP client：stdio / Streamable HTTP | 已实现 |
| MCP 配置、凭据引用、OAuth connector 绑定 | 已实现 |
| MCP catalog pin / drift fail-closed | 已实现 |
| MCP bounded JSON Schema 与调用前参数校验 | 已实现 |
| MCP 外部写 `outcome_unknown` 防重放 | 已实现 |
| MCP server：把 WorkPilot 资料库暴露给外部 Agent | **尚未实现** |
| Skill builtin / user / project 三层目录 | 已实现 |
| Skill 安装、启停、导入、资源读取 | 已实现 |
| 成功 run 后自动蒸馏候选 | 已实现 |
| 候选人工晋升 / 拒绝 | 已实现 |
| 只读候选的可选自动晋升 | 代码路径已实现，**部署默认关闭** |
| 基于自动评测回放的晋升与自动退化 | **尚未实现** |

本文只描述这些能力本身，不把尚未实现的方向写成当前承诺。

---

## 1. MCP server：仍是设计方向

WorkPilot 未来可以把资料库以 MCP tools 暴露给 Claude Code、Claude Desktop 等外部 Agent，候选工具包括：

| 工具 | 设计意图 |
|---|---|
| `search_knowledge` | 返回带 `block_id`、版本、页码和 quote 的混合检索结果 |
| `list_documents` | 按时间、标签或类型列文档 |
| `get_document_outline` | 返回标题树与 block 摘要 |
| `ask_grounded` | 完整的带引用问答 |
| `create_draft` | 只创建待确认草稿，不直接修改用户文件 |

引用元数据必须完整跨过 MCP 边界，写操作也不能把 WorkPilot 的 HITL 责任外包给调用方。

但截至 2026-08-28，仓库中没有 MCP server、`/mcp` server 路由或 stdio server 启动器。上表是后续设计，不属于当前验收范围。当前落地的是下一节的 MCP client。

---

## 2. MCP client：已实现的安全边界

### 2.1 配置不是秘密存储

MCP 配置兼容顶层 `mcpServers` 与 `servers`。其中的凭据只能保存为显式环境变量引用或 OAuth connector id，不能保存明文值；普通 URL、command 和工具策略仍是常规配置。

```yaml
mcpServers:
  docs:
    enabled: true
    transport: streamable_http
    url: ${DOCS_MCP_URL}
    headers:
      # 环境变量的值应包含完整 header value，例如 "Bearer ..."
      Authorization: ${DOCS_MCP_AUTH}
    catalog_sha256: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
    tools:
      search:
        enabled: true
        side_effect: false
        data_scope: corpus_allowed
        when_to_use: 查询已授权的外部文档
        when_not_to_use: 输入包含不应出站的敏感资料

  local-helper:
    enabled: true
    trusted: true
    transport: stdio
    command: local-mcp-server
    args:
      - --api-key=${LOCAL_MCP_TOKEN}
    env:
      ACCESS_TOKEN: ${LOCAL_MCP_TOKEN}
```

已实现的配置门禁包括：

- HTTP / Streamable HTTP URL 必须是 `http` 或 `https`，并拒绝 userinfo、query 和 fragment，空的 `?`、`#` 也拒绝。
- 敏感 header 名称，以及 `Bearer`、`Basic`、`Signature` 等认证形态的值，只能以精确 `${ENV_NAME}` 引用持久化。
- stdio `env` 的值只能是精确环境变量引用。
- `--token value`、`--api-key=value`、`-pvalue`、`--header ...`、`--env KEY=value`、`API_KEY=value` 等常见凭据参数同时覆盖 separated、attached 与裸赋值形式；明文一律 fail-closed，环境变量引用允许。
- 启用 stdio server 必须显式设置 `trusted: true`，因为启动本地进程本身已经越过调用级审批边界。
- OAuth token 只在内存中从加密 secret store 注入，且 connector descriptor 必须明确允许目标 origin；URL 在解密 token 前先校验。

环境变量展开或 OAuth hydration 会在 `McpConfiguration` 上留下运行时 provenance。`McpClientManager` 在创建连接前再次验证：敏感 header、stdio 参数和 env 必须已经由可信加载路径解析，未解析的 `${...}` 或通过未验证 `model_copy` 注入的值不能触达 I/O。

HTTP client 关闭自动 redirect 与系统代理继承；stdio 只继承启动所需的最小环境，再叠加显式配置的 env。

### 2.2 策展、数据出站与 catalog pin

远端 server 返回的工具不会直接进入模型目录。每个工具仍需本地策略明确启用，并填写 `when_to_use` 与 `when_not_to_use`。

规则如下：

1. 未配置或未启用的工具不可见。
2. 未显式声明 `side_effect: false` 的工具按有副作用处理；外部写工具必须逐调用审批。
3. 当前没有逐字段污点追踪，因此只有显式 `data_scope: corpus_allowed` 的工具才能注册；默认 `deny`。
4. 远端工具的名称、描述和 `inputSchema` 组成 catalog digest。配置没有 `catalog_sha256` 时状态为 `catalog_review_required`；digest 不一致时状态为 `catalog_drift`，整个 server 不注册工具，等待人工复审。

catalog pin 固定的是本次本地验证与调用使用的外部契约，不能阻止远端改变实现，但可以阻止名称、描述或 schema 静默漂移后继续进入模型。

### 2.3 bounded JSON Schema

`inputSchema` 是远端控制的不可信输入。当前实现使用 Draft 2020-12 的保守子集，在 catalog 拉取、digest 计算与 registry 注册时编译校验：

- 单 schema 最大 64 KiB，语义深度最大 20、节点最多 2,048、properties 最多 256、组合分支最多 32。
- 只允许有界、无环的本地 `$defs` / `definitions` 引用；外部 `$ref`、不存在的引用和递归引用全部拒绝。
- 未支持的关键字 fail-closed；包括可能带来正则拒绝服务的 `pattern` / `patternProperties`。
- format 使用 allowlist；非有限数值、过深 annotation / `const` / `enum` 等原始 JSON 结构也会在交给 `jsonschema` 前拒绝。

工具真正执行前，参数还会再次按已经 pin 的 schema 本地验证，并施加独立的实例边界：深度、节点数、容器项数、单字符串与总字符串大小都有硬上限。验证发生在授权和远端调用之前；失败错误是固定文本，不包含参数值、schema 攻击内容或 secret。

### 2.4 不可信输出与诊断脱敏

MCP 返回值放在 `untrusted_content` 中，并附带“不能授权或覆盖系统指令”的安全标记。远端 `isError` 内容、响应转换异常和原始 SDK 异常不会原样进入模型。

运行时诊断遵循以下约束：

- stdio stderr 只保留有界 tail，而不是无限累积；reader 退出也有超时，孙进程继承 fd 不会卡死关闭。
- header、env、stdio credential 参数中的已解析 secret 会先加入精确擦除集合，再叠加认证字段和长 opaque value 的通用脱敏。
- health status 只公开脱敏后的有界错误与 stderr tail。
- 对外异常使用固定分类文本，不回显 URL、header、参数、远端正文或 stack trace。

### 2.5 外部写的 unknown-effect

连接失败发生在调用派发前时，远端明确返回 `isError` 时，结果是已知失败，不会误标成 unknown。

一旦 `call_tool` 已派发，下列情况无法证明远端没有完成副作用：

- 连接中断；
- 调用超时；
- 派发后的取消；
- 成功响应已返回，但本地无法安全解析或编码。

这些路径抛出明确的 outcome-unknown 类型，且不会自动重放已经提交的调用。对于外部写工具，Cowork 将同一 `run / plan_step / tool / arguments` 对应的 invocation 持久化为终态 `outcome_unknown`，清除租约并只存固定 receipt。后续即使换 tool call id 或 worker，再次 acquire 同一 invocation 也会被阻断，避免未知远端副作用被执行两次。

`outcome_unknown` 不是“成功”哨兵，也不是普通 `failed`；用户需要先核实远端状态，再决定是否创建语义上全新的操作。

---

## 3. Skill：三层目录与渐进加载

### 3.1 folder-is-truth

当前 Skill 没有 `skills` 数据库表，也没有 embedding 索引。`SKILL.md` 所在目录就是真相：

| 层 | 路径与来源 | 优先级与约束 |
|---|---|---|
| builtin | 随代码提供的只读目录 | 最低；可被同名 user / project 覆盖，但不可删除 |
| user | `cowork_skills_path/<name>/SKILL.md` | 可安装、替换、启停、删除和 ZIP 导入 |
| project | 已授权 workspace 的 `.workpilot/skills/<name>/SKILL.md` | 最高；只能来自当前会话已授权的 project root |

同名优先级为 `project > user > builtin`，多个已授权 project root 同名时取授权顺序中的第一个有效来源。被覆盖的名字记录在 `shadowed`，不会静默出现两份同名 Skill。全局停用是名称级 deny：一旦停用，低优先级同名 Skill 不能 fallback 复活。

catalog 对每个有效 Skill 记录 `name / description / trigger / anti_trigger / tools / origin / sha256`，并计算稳定的 `snapshot_sha256`。

### 3.2 只把摘要放进 prompt

运行时 prompt 只放 Skill 的名称、用途、trigger 与 anti-trigger。完整 procedure 必须通过 `load_skill` 按需读取；资源文件通过 `load_skill_resource` 单独读取。

这些入口统一执行：

- 名称、YAML frontmatter、状态、目录名与文件大小校验；
- 拒绝符号链接和越界路径；
- 资源文件按实际读取字节限流，只允许有界 UTF-8 普通文件；
- project Skill 的来源身份绑定到具体已授权 workspace；
- Skill procedure 与资源始终视为不可信数据，不能自行扩大工具、权限或审批范围。

`load_skill` 成功后，runtime snapshot 记录 `name / origin / source_identity / sha256`，而不是只记一个容易碰撞的名字。

### 3.3 会话级持久 mute 与运行期间冻结

除全局启停外，每个 conversation 可以持久 mute 某个 Skill 名称。mute 过滤的是与 runtime 完全相同的三层 effective catalog，并且不能重新启用一个已经全局停用的名字。

为避免一个 run 前后看到两套 procedure：

- conversation mute 只能在两次 run 之间修改；会话仍有任务运行时更新返回冲突。
- run 启动时绑定 effective catalog、mute 列表与 snapshot hash；同一进程内不会热替换 registry。
- checkpoint 恢复时会把已加载 Skill 的完整身份与当前 catalog 对比。Skill 被修改、删除、停用、mute 或来源改变时，旧身份进入 `invalidated`，系统注入 countermand，禁止继续依赖历史 procedure。
- 对仍存在但内容已变化的 Skill，必须重新调用 `load_skill` 后才能使用新版本；不可用的 Skill 不能从旧 checkpoint 复活。

这提供的是可验证的运行期冻结与漂移撤回，不是尚未实现的历史版本库。当前 snapshot hash 绑定有效 catalog 条目及其 `SKILL.md` 内容哈希 / origin，不封存按需读取的资源字节；仅资源变化不会改变该 hash。系统也没有文件 watcher 或根据旧 hash 恢复历史 catalog 的能力。

---

## 4. Skill 蒸馏与晋升：当前实现

### 4.1 候选目录与作业队列

成功结束的 Cowork run 在启用蒸馏时按 run id 幂等入队。候选同样使用 folder-is-truth：

```text
<skills_candidates_root>/<capability_key>/SKILL.md
<skills_candidates_root>/<capability_key>/meta.json
<skills_candidates_root>/<capability_key>/evidence/<run_id>
<skills_candidates_root>/.queue/<run_id>.json
```

`evidence/<run_id>` 是一次独立成功的一枚空文件，使用 `O_CREAT|O_EXCL` 去重；候选状态为 `collecting / needs_review / promoted / rejected`。同 capability 的 `collecting` 候选会合并更新；每个新 run 另加一枚独立证据，同 run 重试不重复计数。证据文件不需要跨数据库和文件系统双写。

### 4.2 隐私安全蒸馏

蒸馏不是把完整 checkpoint 交给模型。来源快照只包含有界的 goal、最终消息、成功工具名，以及注册表在 run 结束时给出的 promotion risk snapshot。

持久化前有两层门禁：

1. **来源门禁**：凭据、secret、高敏事实和直接身份信息会让作业 fail-closed；拒绝后的 tombstone 清空 goal、最终消息与工具列表，不保留原文。
2. **候选门禁**：模型只能输出固定 JSON 契约，且工具必须来自本次实际成功的 tool result。MCP、shell、授权控制面等禁止自动固化；模型契约要求步骤去掉文件名、路径、日期、人名、账号和本轮具体结果，服务端再确定性扫描 prompt injection、凭据、PII 与高敏内容。

失败日志和重试 tombstone 只保存有界、脱敏的分类，不把 provider 异常中的来源正文写回。

### 4.3 人工晋升是默认路径

当前默认配置是：

- `skill_distillation_enabled = true`：成功 run 可以生成并累积候选；
- `skill_auto_promotion_enabled = false`：候选不会自动安装，等待用户人工晋升或拒绝；
- `skill_promotion_min_evidence = 3`、`skill_promotion_min_confidence = 0.82`：作为可选自动路径的确定性阈值。

这里的 confidence 是模型在候选契约中给出的自报值；合并候选时保留较高值。它不是 replay 评测结果，也不是实测成功率。

promotion risk 不信模型声明，而是读取当次 registry 的实际契约。只有 `local + read + effect=none + 无审批 + 静态只读 capability` 的工具能被证明为只读；未知工具、动态 capability、持久控制面和任何副作用工具都会把候选送到 `needs_review`。

如果管理员显式打开 auto promotion，只有达到证据与置信度阈值、且所有工具都被证明为只读的候选可以自动安装。这个路径目前不是“自动评测晋升”：它没有在来源任务或邻近任务上做 paired replay，也没有比较无 Skill 基线，因此部署默认关闭。

人工晋升可以显式接受尚未达到自动阈值的 `collecting` 候选，也可以审查后接受 `needs_review` 候选；它不会绕过 Skill 格式、隐私扫描、同名覆盖保护和签名安装门禁。

### 4.4 签名 provenance 与覆盖边界

自动蒸馏安装时会创建内部 HMAC provenance receipt。签名 payload 绑定：

- `origin = auto_distilled`；
- Skill name；
- capability key；
- 精确 `SKILL.md` SHA-256。

签名键由本地 secret store 派生，不写入 Skill 正文。后续自动更新只接受签名有效、内容哈希一致、name 与 capability 都匹配的已有自动 Skill；在正文里伪造 `origin: auto_distilled` 标记没有作用。候选阶段的 evidence run id 仅是独立证据标记，不在这份 receipt 的签名 payload 内，也不会被复制进已安装 Skill。

自动晋升不会覆盖人工 user Skill，也不会遮蔽同名 builtin Skill。人工晋升仍复用同一签名安装入口，使未来自动更新无法把一份人工编辑或被篡改的 Skill 当作自己先前生成的内容。

---

## 5. 评测层：已验证什么，仍缺什么

当前自动化测试覆盖的是安全与一致性契约：

- MCP 配置凭据不落盘、不进公开状态与错误；URL、stdio 参数和 runtime provenance 在 I/O 前 fail-closed。
- 危险、过深、过大或递归的 MCP schema 被拒绝；不合法参数不会触达 manager。
- catalog 未 pin 或 drift 时工具不注册。
- 外部写 unknown-effect 进入不可重放终态，同 invocation 的第二次调用不触达远端。
- Skill 三层优先级、全局停用、conversation mute、project 授权与 snapshot 稳定。
- 已加载 Skill 漂移后的 invalidation / countermand。
- 蒸馏隐私拒绝、成功工具约束、只读 promotion risk 与签名 provenance。

正式 eval run 会关闭蒸馏，避免评测执行本身生成新候选；这不等于已实现一般化的语料来源过滤与污染审计。

以下能力尚未实现，不能把阈值式 auto promotion 等同于它们：

1. 候选 Skill 在来源任务和相邻任务上的自动 replay；
2. 开 / 关 Skill 的 paired 基线比较与非退化门禁；
3. Skill 实际采用率、成功增益、步数和成本的持续统计；
4. 工具或语料变化后的自动退化检测、降级和撤回；
5. 对 eval run 与测试语料来源的系统化污染审计。

后续评测至少应记录：

| 指标 | 定义 |
|---|---|
| `skill_reuse_rate` | run 中加载并实际遵循 Skill 的比例 |
| `skill_hit_precision` | 加载的 Skill 确实适合该任务的比例 |
| `task_success_delta` | 同任务开 / 关 Skill 的成功率差 |
| `step_efficiency_delta` | Skill 对步骤数、调用数和成本的影响 |
| `pollution_incidents` | 坏 Skill 导致的回归次数 |
| `tool_selection_accuracy` | 有无外部干扰工具时的选择准确率 |

自动评测晋升必须满足“不低于无 Skill 基线”，自动退化也必须使用同一套冻结任务与可回放证据。在这套闭环完成前，人工晋升和默认关闭 auto promotion 是正确的生产边界。

---

## 6. 风险与当前控制

| 风险 | 当前控制 | 尚缺 |
|---|---|---|
| MCP 配置泄漏凭据 | env / connector 引用、runtime provenance、公开状态与错误脱敏 | 更完整的 secret provider 抽象可后续扩展 |
| MCP catalog 投毒 | schema 安全编译、catalog pin、drift 禁用 | 远端实现本身仍需信任与审计 |
| MCP 参数资源耗尽 | schema 与实例双重复杂度上限 | 按 server 的累计 CPU 指标 |
| 外部写重复执行 | 逐调用审批、invocation ledger、`outcome_unknown` 终态 | 远端业务级核实与补偿流程 |
| 语料外流 | `data_scope` 默认 deny；无字段级追踪时要求显式 `corpus_allowed` | 字段级 taint / DLP |
| 坏 Skill 污染后续行为 | 候选隔离、人工晋升默认、只读 auto gate、签名 provenance、会话 mute | 自动 replay 晋升与退化 |
| Skill 漂移破坏可复现性 | snapshot、loaded identity、resume countermand、运行中禁止改 mute | 独立的历史版本与评测快照仓库 |
| 蒸馏持久化隐私数据 | 来源与候选双重确定性门禁、拒绝 tombstone 清空原文 | 更系统的污染审计与可解释拒绝报告 |

---

## 7. 下一步

只保留仍然真实的三项后续工作：

1. 实现只读优先、引用元数据完整的 MCP server，并单独验证权限、限流与引用一致性。
2. 为 Skill 建立冻结任务集、无 Skill 基线和 paired replay，完成自动评测晋升门禁。
3. 基于实际加载与执行结果记录 Skill 效果，在可靠统计基础上实现退化检测和安全撤回。

在这三项完成前，现有边界保持不变：MCP server 不宣称可用；默认配置下 Skill 自动蒸馏只生成候选；人工晋升是默认，阈值式 auto promotion 保持关闭。
