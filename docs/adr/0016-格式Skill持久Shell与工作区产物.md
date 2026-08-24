# ADR-0016 格式 Skill、持久 Shell 与工作区产物

**状态**：已采纳
**日期**：2026-08-22

## 背景与约束

WorkPilot 已经为 Word/Excel 建立过 `inspect_office_file`、`edit_word`、`edit_excel` 等正式
工具协议，并为原生交付物维护另一套生成 schema。实践中这些协议把格式库的能力压缩成少量
JSON 操作：支持一个新图表、模板或 OOXML 结构都要同时修改 schema、执行器、提示词和测试。
模型还必须遵守“扫描 → inspect → edit”的人为状态机，错误时容易重复调用而不是解决任务。

产品同时有以下硬约束：

1. WorkPilot 是本机桌面 Cowork，输入、字体、模板和企业 CLI 本来就在用户电脑上。
2. 用户希望处理 DOCX、XLSX、PPTX、PDF 等开放格式，而不是等产品逐个增加窄工具。
3. 本地代码执行不能等同于全盘 Full Access；目录、能力、审批、幂等和审计边界必须保留。
4. 产物仍要进入统一 Artifact 区，不能让 Shell 生成文件后在产品里“消失”。
5. 同一任务会连续准备脚本、切换目录或激活环境，跨轮上下文连续性有实际价值。

## 决策

退役 Office 格式专用正式工具，采用 **格式 Skill + 本机会话级持久 Shell + 工作区产物发现**：

- 内建 `docx`、`xlsx`、`pptx`、`pdf` Skill，指导模型选择 Python/CLI、保留源文件、用临时
  文件验证并交付新产物；项目 Skill 可随工作区覆盖这些流程。
- 模型用通用 `list_files`、`read_text_file`、`write_text_file` 和 `run_shell` 完成处理，不再
  下发段落/单元格级 Office schema。
- 选择本机持久 PTY。活进程内保留 cwd/env/venv；重启只从最后 cwd 重生并明确承认 env 丢失。
- `run_shell` 仍要求独立 `shell.execute`，cwd 必须属于已授权读写 root，命令继续经过可信
  allowlist、逐次审批、超时、进程组取消与 `tool_invocations` 租约。
- 前台 Shell 调用执行前后对 root 做有界快照。新建或变更的支持格式经格式重开、大小/解压
  上限、SHA-256 和 MIME 判定后自动登记 Artifact；扫描失败不得触发命令重放。
- 新 root 只派生 `filesystem.read` / `filesystem.write`。旧数据库中的
  `office.word.edit` / `office.excel.edit` 只保留读取与撤销兼容，不再展示或允许新申请。

## 考虑过的替代方案

| 方案 | 优点 | 放弃理由 |
|---|---|---|
| 继续扩充 Office 正式工具 schema | 参数可验证，操作面最窄 | 每个格式特性都形成协议债；无法自然复用 Python/CLI 和项目模板 |
| 每轮隔离沙箱，只挂载任务目录 | 环境天然可回收，多租户隔离更强 | 桌面单用户形态收益有限；反复丢失 cwd/env/脚本上下文，字体、模板和本机 CLI 接入成本高 |
| 无限制本机 Shell | 实现最简单，能力最强 | 目录与外部副作用不可控，不满足既有 capability 和审批承诺 |
| 只靠 Shell 输出路径，由模型显式登记产物 | 无需扫描 | 模型漏报时客户端没有交付物；模型提供的 MIME/路径也不能直接信任 |
| 继续用格式专用工具，Shell 仅作兜底 | 迁移风险小 | 形成两条功能重叠的路径，提示词与评测仍会把模型引回旧状态机 |

## 接受的代价

- 失去工具层的段落/单元格操作白名单与自动 baseline 冲突协议。格式 Skill 必须明确“默认新文件、
  覆盖先备份、临时写入、重开验证”，复杂编辑还应依赖版本控制或人工预览。
- Python/CLI 的能力面比 Office 专用执行器更大，因此每次运行仍可能需要命令审批；允许规则必须
  从用户实际看到的审批 payload 派生，不能由模型自行扩大。
- 自动发现是有界且后缀驱动的。后台任务稍后生成的文件不会自动登记，超限工作区会报告扫描
  截断，格式可打开也不代表视觉布局正确。
- 开发态可以复用 backend venv；桌面发布必须携带 Python 或等价格式 CLI。禁止在任务执行时
  静默联网安装依赖。

## 后续影响

1. `inspect_office_file`、`edit_office_file`、`edit_word`、`edit_excel`、
   `list_office_files`、`create_native_artifact` 不得重新进入工具注册表。
2. Office 评测必须要求加载格式 Skill、调用前台 Shell、验证源文件保护与 Artifact 登记；退役
   工具可以出现在 `forbidden_tools`，不能出现在 `required_tools`。
3. Artifact 预览保留为独立读能力；“如何编辑格式”与“如何展示产物”不再共用执行器。
4. 若未来进入远程多租户执行，应新增真正的容器/VM 执行后端；不能把当前本机信任模型直接搬
   到服务器。是否切换由部署形态决定，而不是由文档格式决定。
