# ADR-0017 Agent 权限按动作与资源作用域授权

**状态**：已采纳

**日期**：2026-08-23

## 背景与约束

WorkPilot 是单用户桌面应用，但 Agent 仍会处理不可信网页、文档和工具输出。旧模型把
`shell.execute`、`browser.control`、`external.action` 和 `network.read` 当成宽泛开关：
一次授权可能同时覆盖宿主执行、浏览器写入、外部删除和任意域名访问。常驻审批又允许按
工具名或命令前缀匹配，参数追加后仍可能命中。

这里要防的不是用户 A 越权访问用户 B，而是被 prompt injection 影响的 Agent 将一次合理授权
扩张成宿主机执行、数据外传或重复副作用。

## 决策

权限同时绑定动作、目标与本次调用参数，统一在工具注册表的副作用边界校验：

| 能力面 | 新 capability | 边界 |
|---|---|---|
| 命令执行 | `sandbox.execute` / `host.execute` | 隔离容器和宿主机明确分开；隔离后端不可用时拒绝，不回退宿主机 |
| 浏览器 | `browser.read` / `browser.write` / `browser.destructive` | 导航/观察、填写、点击/上传/下载分别授权 |
| 外部系统 | `external.read` / `external.write` / `external.destructive` | 查询、创建/更新、删除分别授权；DELETE 动态解析为 destructive |
| 网络 | `network.fetch` | grant 必须带精确 origin 或 domain scope；每次请求和每个重定向重新校验 |

网络 scope 只接受 `origin:https://host[:port]` 或 `domain:example.com`。origin 严格匹配协议、主机和
端口；domain 可覆盖其子域，但不接受 IP、凭据或单标签主机。旧 capability 只用于读取历史 grant，
并且只能单向满足对应的新能力；新 API 不再创建宽泛 grant。

常驻审批只允许两类规则：

- `argv_pattern`：版本化 JSON，完整 argv 精确匹配，可绑定精确 cwd；含 Shell 操作符的命令不能形成规则。
- `action_target`：版本化 JSON，精确匹配工具、action/method 和经过选择的目标字段。

工具调用取得 capability、路径授权、一次性审批或常驻规则后生成 `authorization_receipt`。回执包含
规范化参数哈希、grant/rule 标识、资源 scope、审批来源、调用标识和签发时间；它随工具结果与事件保存，
用于回答“这次调用为什么被允许”。参数在批准后发生变化时，执行入口按哈希拒绝。

`run_sandbox` 使用本机 Docker 或 Podman 的 argv 接口，默认 `--network=none`、只读根文件系统、
drop all capabilities、no-new-privileges、非 root 用户、资源上限，并只将已授权 cwd 读写挂载到
`/workspace`。`run_shell` 明确代表宿主执行，仍保留独立审批和路径授权。
镜像使用 `--pull=never`；owner 必须在启用前显式预拉取配置的镜像，Agent 不能借一次
`sandbox.execute` 授权触发宿主侧联网下载。

## 考虑过的替代方案

| 方案 | 优点 | 放弃理由 |
|---|---|---|
| 单一 `full_access` / `shell.execute` | UI 与模型最简单 | 无法区分隔离执行和宿主执行，一次授权的爆炸半径过大 |
| 仅按工具名授权 | 实现成本低 | 同一工具的 GET 与 DELETE、不同域名或目标后果不同 |
| 命令字符串前缀 | 使用方便 | 追加参数、换 cwd 或拼 Shell 操作符后仍可能继承授权 |
| sandbox 不可用时回退 host | 成功率高 | 把环境故障静默变成权限升级 |
| 只记录“已批准”布尔值 | 存储简单 | 无法审计具体参数、grant、scope 与规则来源 |

## 接受的代价

- 用户首次访问不同 origin/domain 时会看到更多授权请求。
- 外部连接器必须正确声明 read/write/destructive；新增工具漏声明会在注册或执行阶段失败。
- 本机没有可用 Docker/Podman 或指定镜像时，`run_sandbox` 不能执行。
- 历史宽泛 grant 在迁移期仍被兼容读取，需要后续版本提供显式清理和迁移提示。

## 后续影响

1. 新工具必须声明 capability；scoped capability 还必须提供资源目标解析器。
2. 网络客户端必须把重定向后的 URL 再送回授权器，不能只检查初始 URL。
3. 审批 UI 只能从用户实际看到的 inbox payload 派生常驻规则。
4. 回归测试固定覆盖过期、撤销、批准后参数变化、软链竞态、重复副作用和 Shell 越界。
5. 容器隔离是最小后端；后续可替换为 microVM/远端 runner，但不得改变 fail-closed 语义。
