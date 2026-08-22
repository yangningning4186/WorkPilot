# 16 · OpenWorker P0/P1 能力对齐

> 状态：2026-08-22（第三轮）。本文只记录已经进入代码、客户端和测试的能力；明确的安全边界不包装成缺陷。
>
> **这张表只覆盖 OpenWorker 那一半。** 另一半（DeepTutor 的沉浸阅读）见文末的
> [§ DeepTutor 侧对齐](#deeptutor-侧对齐)，两者合流的理由见
> [ADR-0013](adr/0013-沉浸阅读作为工作模式而非第二条产品线.md)。

| 能力面 | P0/P1 实现 | 关键边界 |
|---|---|---|
| Provider 与密钥 | OpenAI、Anthropic、Gemini、DeepSeek、Qwen、Ollama、OpenAI-compatible；会话级 Provider/模型切换；Fernet 密文和数据库外 0600 主密钥 | Anthropic/Gemini profile 只接管对话；资料库 embedding **直连本机 Ollama、不过网关**（约束 1 的唯一豁免，[04 §3.8](04-知识与阅读设计.md)） |
| 连接器与 OAuth | GitHub、飞书、企业微信、微信公众号、腾讯文档账户 CRUD、OAuth state、加密 token；固定官方主机的读/写 API 工具 | 不支持个人微信模拟登录；外部写动作逐次审批 |
| MCP client 管理 | stdio/Streamable HTTP、服务 CRUD、OAuth 绑定、探测、目录哈希固定、逐工具数据域与副作用策略 | 未策展、目录漂移、`data_scope=deny` 均不可见；stdio 需显式信任 |
| Skills 生命周期 | `SKILL.md` 安装、更新、启停、删除、ZIP 安全导入、资源读取、快照哈希和按需加载 | 自动蒸馏/评测晋升仍属于技能自进化后续阶段，不与人工生命周期混称 |
| Web 与浏览器 | 公网搜索、网页/远程 PDF 读取；`network.read` + 会话级 `browser.control` 双授权下的受控浏览器导航、编号控件点击/输入/选择、后退、页内查找、下载与截图 | 浏览器无持久 Cookie/登录态；每跳重新做 SSRF/DNS 钉扎；session 绑定会话，空闲 TTL 顺延但绝对 TTL 不顺延；动作必须逐个调用，本地文件上传仍逐次审批，下载/截图另需目录写授权 |
| 原生交付物 | 原子生成 DOCX/XLSX/PDF，覆盖需 baseline，保留有界备份；Artifacts 内联 PDF、语义预览 DOCX/XLSX | 语义预览不承诺替代 Office 的像素级排版渲染 |
| 审批粒度 | 三档：计划模式（只读）· 逐次审批（默认）· 免审批（会话设置里由用户显式开，模型没有对应工具）。另有常驻规则三种粒度——整只工具 / 精确目标 / `run_shell` 的 argv 前缀——只能在审批卡片上由用户勾选产生 | 规则只省掉「再问一次」，**不放大 capability**：没有 `shell.execute` 的会话攒再多规则也跑不了命令。argv 前缀匹配要求命令里没有 shell 操作符，否则 `npm test` 的授权会被 `npm test && rm -rf ~` 白嫖走。规则必须从 inbox 那份 payload 派生，因为那正是用户在卡片上看到的内容；事后从模型输入重算，用户点的和最终生效的就可能不是同一条。每一次免审批放行都发 `approval.waived` 事件，否则用户只会看到一条命令凭空执行了 |
| 仓库自带白名单 | 仓库在 `.workpilot/config.toml` 的 `[shell].allow` 里声明命令前缀，**只在用户信任过那个规范化路径之后**生效；条目数有上限，带操作符的条目一律拒绝并回带原因 | 仓库自己说了不算：clone 一个陌生仓库就等于执行它声明的命令，那是一条从「读代码」到「跑代码」的静默升级。信任跟着路径走而不是配置快照，因为「每次仓库改 allowlist 都重新问一遍」会把信任变成又一个闭眼点过的弹窗 |
| 计划的常驻授权 | `create_schedule` 接受 `standing_approvals`，批准创建的那一刻派生 scope=schedule 的规则；删除计划连带删除 | 不做成会话级：手工发起的对话不该悄悄继承一批自己从没看过的授权。不列出来的话，一条每天七点跑的计划每天都会停在「允许 `npm test` 吗」上——那就不是无人值守 |
| Scheduler / Inbox | 单次/五段 cron、离线最多补跑一次、重叠保护、按持久化 `next_run_at` 轮询补偿、立即运行、暂停/恢复/删除；跨会话 Inbox | Unattended 不自动续权；提问、目录/能力、Shell 与外部动作都会安全暂停 |
| Shell | 同步 `run_shell` 之外增加后台任务：`run_in_background` 起、`shell_task_output` 轮询增量、`shell_task_kill` 按进程组回收；并发数、输出字节、绝对存活时间三重上限 | 不做持久 shell（保留 cd/venv 的交互式会话）：run 会在 waiting_human 处暂停并可能恢复到另一个 worker，那边 PTY 的 cwd 是错的而命令照跑。后台任务同样不跨重启存活，但失败是显式的 |
| 只读版本视图 | `git_status` / `git_diff` / `git_log`，固定 argv、不拼 shell 字符串、输出按已授权目录收窄 | 不走 `run_shell` 是为了拿掉审批摩擦又不给写操作留入口：放行整个 `git` 等于同时放行 `push`、`reset --hard`、`clean -fd`。`git -C` 会顺着找到仓库根，所以每条命令都追加 `-- <已授权目录>` pathspec，否则会吐出用户没授权的那半个仓库的差异 |
| 代码搜索与读取 | `search_files` 有 ripgrep 时走 ripgrep（尊重 `.gitignore`、跳过二进制与 `node_modules`），否则回落纯 Python；`read_text_file` 返回 `行号<TAB>` 前缀并在截断时给出续读指令 | ripgrep 的 include glob 是一层 override，把 `pattern` 交给 `--glob` 会连 `.gitignore` 一起绕过（`--glob '*'` 把整个 `build/` 列回来），所以 pattern 留在 Python 侧过滤。行号的税是明确的：模型可能把前缀抄进 `replace_in_file` 的 `old_text`，工具描述里必须显式写清楚它不属于文件内容 |
| 局部编辑 | `replace_in_file` 精确文本替换，默认要求全文唯一命中，命中多处需显式 expected_count | baseline_sha256 挡的是并发写，挡不住「只读了前 500 行就整份重写」——后者校验照样通过而文件后半段被静默丢掉 |
| 自唤醒 | `sleep(seconds|until)` 把 run 挂起为 sleeping，到点由调度 tick 原子领取并恢复同一份 checkpoint；`wake_on(task_id)` 挂在后台 shell 任务的结束事件上，零模型调用地等它跑完 | 与 waiting_human 分开：那个在等人、界面要提示，这个在等时间、不需要人。墙钟预算按分段计时，睡眠期间没有开着的分段，睡一小时不烧预算。**`wake_on` 刻意不挂起 run**：挂起会释放 worker，而后台进程活在这个 worker 的内存里，换一个 worker 恢复就再也读不到输出了——所以它占着一个 worker 槽位，换来的是「等到的那一刻就是任务结束的那一刻」。同理，本会话还有后台任务在跑时 `sleep` 会被直接拒绝并指向 `wake_on` |
| 空转熔断 | 按调用签名（工具名 + 规范化参数）计数，同一签名超过 3 次不再执行并回一条可执行纠正指令；连续 2 轮整批都是重复调用就收回全部工具，做一次不带 tool-calling 的补全强制交付回答 | 判据是签名不是工具名：读十个不同文件是正常工作，读同一个文件十遍不是。同批里只拒重复的那几个，否则一次空转会放大成一轮空转。拒绝只是提示——评测里模型无视了 22 次直到预算熔断，所以第二层必须把工具拿走 |
| 上下文装配 | system prompt 只放一次 run 内不变的内容（基座、工具说明、环境块、记忆快照）；任务清单、当前目录、**已授权能力**、计划模式提醒每轮重算，挂在 outbound 视图末尾的 session_state 块里 | 分界依据是「这一次 run 里会不会变」而不是主题：provider 前缀缓存从 system 起算，会变的内容放进去等于每轮作废整段前缀；末尾块只让自己失效。临时块不写回 canonical。能力块同时写明「已授予不等于免审批」与「能力按工具划分不按后果划分」——不写，模型会自己推断「删文件属于写」从而去要一个用不上的 filesystem.write |
| 消息面（出站） | 命名 Inbox + 投递绑定：审批与提问镜像到飞书群，离散选项渲染成卡片按钮，点击就地解析同一条 item。会话按「自己的覆盖 → default」两级路由 | 应用内 Inbox 永远是 store of record，绑定只是镜像：投递失败只是没镜像出去，请求本身还在。按钮 value 里编着 item id，所以不需要回复里的 `[id]` 标记也不需要 thread 反查。开放式提问不给按钮——一条自由回复既没有身份也没法校验格式 |
| 消息面（入站） | 频道订阅把群消息带进会话；@机器人在无人订阅的群里会开一个新会话并**拥有**那条 thread；忙就 steer、闲就起一轮，与自唤醒同一条路径 | 订阅与 Inbox 绑定方向相反，别指到同一个频道——那是自问自答的回路。thread 键就是地址串本身（`feishu:oc_x:om_y`），发消息、查会话、判定授权共用一份真相；分别拼一次迟早出现「看起来一样但比不相等」的地址。事件回调没配 `encrypt_key` 就整个关闭，配了就逐条验签 |
| 死信 | 无处投递的入站消息与失败的后台轮次进 `cowork_unrouted`，界面里可读，按条数封顶 | 它是可见性设施不是队列，条目不会被重投。没有它，「我在群里说了一句，什么都没发生，也查不到为什么」就是彻底静默的失败 |
| 长期记忆 | `remember` / `memory_update` / `memory_forget` / `memory_read`；global / workspace / conversation 三级作用域，软删除，run 起始快照进 system prompt；客户端记忆面板与内联撤销 | **2026-08-22 起与 RAG 的 memory 合并成一张表**：作用域抄 OpenWorker 的扁平结构，改写用本项目的时序有效性（不覆盖，只失效——ADR-0005），比两个参照物都多一层「当前 / 历史」。去重不用向量：把最近 N 条活跃记忆整批给模型，让它指名道姓选一条改。只注入本会话可见的那部分 |
| 计划模式 | 会话发起时可选；计划阶段只下发只读与交互工具，`propose_plan` 提交方案后暂停，批准即翻转运行时模式并把步骤变成任务清单；客户端计划卡片支持批准或带修改意见退回 | 批准是 checkpoint 里 `mode` 的翻转，不是 prompt 约定：未批准前写工具既不下发，执行边界也拒绝；准入判据是 `risk`/`execution` 而不是工具名单 |
| 任务清单 | `todo_write` 整份替换的 pending/in_progress/done 清单，存进 checkpoint、每轮重发在末尾临时块里、前端独立渲染 | 与 `agent_plan_steps` 并存不互相替代：前者是模型主动声明的计划，后者是 runtime 从 tool call 派生的事后日志 |
| 只读子 Agent | `explore` 独立上下文、共享预算、轮次/调用上限、证据工具记录 | 过滤所有副作用、Shell 与 `external.action`，当前不开放可写子 Agent |
| 工具规模治理 | 核心目录、按目标相关选择、`search_tool_catalog` 动态激活；历史 tool_call 引用过的 schema 跨话题保留 | 目录按轮重算且有上限，不因某个工具曾被下发过就永久驻留；只有历史调用过的和显式激活的是单调的 |

## 与 OpenWorker 仍存在的差异

不是每一条差距都值得抹平。这里只记**刻意**的分歧，以及还没做的那部分。

| 面 | 现状 | 判断 |
|---|---|---|
| 持久 shell | 只做后台任务，不做保留 cd/venv 的交互式会话 | run 会在 `waiting_human` 处暂停并可能恢复到另一个 worker，那边 PTY 的 cwd 是错的而命令照跑。后台任务同样不跨重启，但失败是显式的 |
| 消息传输 | 只接飞书 | 路由层传输无关（`sender` 注入、按钮只认 `(label, value)`），加一个平台是再写一个适配器。先接飞书是因为它已经是本项目的官方连接器 |
| 连接器广度 | 5 个官方连接器 vs 对方 40 个 descriptor | 这是纯工程量，不是设计差异 |
| 用户本地 risk override | 用会话级的「整只工具不再询问」规则替代，没有独立的全局 override 表 | 对方的 override 是全局的、glob 匹配的；这里做成按会话、精确工具名，代价是每个会话要点一次，收益是撤销点只有一处 |
| 多 persona / 多入口 | 单一 Cowork runtime，但有 `office` / `reading` 两档工作模式 | 分档的依据不是 persona 而是**玩法**：两档共用一个 run、一套审批与预算，只有 system prompt 里那段 playbook 和阅读器面板不同（ADR-0013） |
| STT、TUI、应用自动更新 | 没有 | 还没做 |
| 语义缓存 | 没有，且**已决定不做** | 错误命中会安静地返回一个"看起来对"的答案。在一个把接地当核心承诺的产品里，为省一次调用引入一条会静默说谎的路径不划算（[07 §6](07-模型路由与成本.md)） |
| 请求层限流 | **没有**（随 Redis 一起删掉了） | 桌面形态下没有匿名公网请求，但这是一张欠条，见 [12 §2.2](12-安全与部署.md) |

统一安全不变量分两层。**注册表入口**（`CoworkToolRegistry.execute`）校验 capability 与
`extra_capabilities`、一次性 call-id 审批、裁剪目录的 `allowed` 白名单，并在副作用发生前取得
`tool_invocations` 租约——调用方、恢复路径、Scheduler 与子 Agent 都走这里，没有旁路。
**主循环编排层**（`runtime.decide`）额外强制"独占工具必须单独调用"；这是一批调用之间的
约束，单次执行入口在语义上无法校验它。


---

## DeepTutor 侧对齐

沉浸阅读那一半。参照 DeepTutor，但**寻址抽象是自己的**（locator），因为它同时要服务
"模型引用第 12 页"和"阅读器滚到第 12 页"两件事。

| 能力面 | 实现 | 关键边界 |
|---|---|---|
| 材料与寻址 | 工作区文件 → `app/ingest/` 解析 → unit 序列，PDF 按物理页、文本/MD 按加权字符切节；一切按 1-based **locator** 寻址 | 空页也占一个 locator——跳过会让此后所有页码整体偏移一位，而且没有任何迹象表明出错，这是这一层最容易犯且最难发现的 bug |
| 读 | `material_outline`（结构）· `search_material`（三层匹配定位）· `read_material`（按 locator 取原文） | 每一只的返回都**自带 locator**，所以"这句话出自第 12 页"是取证据的副产品，不是模型事后要记得补的一句话。搜索返回的是开窗片段，可能从句子中间断开——**没读过就是不知道** |
| 匹配 | `exact` → `normalised`（折叠空白、软化标点） → `terms`（按命中词数排序），并把命中在哪一层告诉模型 | 从 PDF 复制的句子在原文里常被硬换行截断；自然语言问句在前两层必然全落空，没有第三层就等于告诉模型"文档里没有" |
| 驱动阅读器 | `reader_goto(path, locator, quote)` 让用户侧面板翻页并高亮 | **引文对不上时照样翻页、只是不高亮**。用中文问英文论文时模型给的"引文"是它自己的翻译，永远不可能逐字命中；为此拒绝跳转会让阅读器看起来是坏的。会持久化的批注是更强的承诺，那一档必须拒绝——留到阅读器面板落地时再加 |
| 溯源 | `verify_quote` 命中哪个 `ParsedBlock`，就把那个 block 的 `locations[]`（页码 / 归一化 bbox / 页面尺寸 / 旋转 / 坐标原点）原样交出去 | 高亮几何是解析的产物，不是前端猜出来的（约束 3）。KB 那条路只到页级，因为切分器按字符窗口切、块边界对不上了 |
| 玩法注入 | `work_modes.render_work_mode_block` 只在用户选了阅读档时把 playbook 写进 system prompt | 工具常驻是对的（模型随时可能要读一份文档），但**玩法**不是。无条件注入实测的后果：模型在非阅读任务里也开始给每句话标页码 |
| 授权 | 每只工具都收 `path` + 声明 `filesystem.read`，注册表在**每一次**调用上重跑目录授权 | DeepTutor 用服务端注入 `material_id` 换"模型不能乱指文件"；这里由既有 capability 系统提供，而且更严——会话中途撤销授权后，模型不会继续拿着一个仍然有效的句柄 |

### 与 DeepTutor 仍存在的差异

| 面 | 现状 | 判断 |
|---|---|---|
| 阅读器面板 | 契约已定（`reader_goto` 的事件），前端面板**尚未落地** | 这是当前最大的一块缺口：工具已经在发翻页指令，但没有地方显示 |
| 持久化批注 | 没有 | 它是比跳转更强的承诺，引文对不上就该直接拒绝。加了也没有地方显示，等面板 |
| 多层记忆（L1 JSONL trace / L2·L3 markdown + 脚注） | 没有，用的是扁平表 + 时序有效性 | 对方那套的价值在"历史活在文档修订里"，这里由 `invalid_at` / `superseded_by` 提供，查询更直接 |
| builtin skill 层 | 只有单层用户 skill，没有随产品出厂的只读 builtin 层 | 对方的 `skill-creator` 本身就是一个 builtin skill，这个思路值得抄，还没做 |
| 阅读评测 | **一条都没有** | `locator_accuracy` / `quote_verifiability` / `read_before_claim` 三个指标已定义（[04 §5](04-知识与阅读设计.md)），未实现——违反约束 6，见 [06 §4.5](06-评测体系.md) |
