# ADR-0015 用描述层扩展连接器、工作能力与 Persona

**状态**：已采纳
**日期**：2026-08-22

## 背景与约束

连接器平台、`office/reading` 工作模式和 Skill 来源原本分别靠硬编码集合、runtime 分支与
builtin/user 两层目录扩展。继续加飞书域、深度研究、会议复盘或项目流程，会让同一项能力
同时修改账户校验、OAuth、工具装配、system prompt、pre-loop 和前端常量。

现有安全边界不能被新抽象替代：工具 capability、计划模式、逐次审批、checkpoint 和
`tool_invocations` 租约已经覆盖恢复与 Scheduler 路径。

## 决策

引入三种只描述、不另造执行器的扩展点：

1. `ConnectorDescriptor` 统一声明平台 catalog、官方主机、鉴权、OAuth adapter、默认 scope、
   能力与专用工具 registrar。
2. `WorkCapability` 声明 `system_block / owned_tools / pre_loop / exclusive`；WorkMode 只负责
   激活哪些 Capability。
3. Skill 采用 project > user > builtin 三层；轻量 Persona 组合稳定提示块、工具 pattern、
   默认审批档、推荐连接器与推荐 WorkMode。

三者都只能装配或收窄能力。工具执行仍统一进入 `CoworkToolRegistry.execute`，Persona 与
Capability 都不能授予目录、全局 capability 或审批规则。

## 考虑过的替代方案

| 方案 | 优点 | 放弃理由 |
|---|---|---|
| 每加一个平台继续改 `if account.kind` | 当次代码最少 | 平台知识散落在存储、OAuth、请求、工具和 UI；漏改一处只会在运行时暴露 |
| 为研究/会议各复制一条 runtime | 模式彼此隔离 | 权限、恢复、审批和租约会出现多份实现，安全修复必须同步多处 |
| 先做 Skill/Persona 市场 | 分发体验完整 | 当前更缺仓库内可版本控制的流程与可解释角色；市场还会提前引入签名、更新和信任问题 |
| Persona 可以授予工具或免审批 | 一键角色更强 | 把产品选择变成静默提权；用户无法分清“推荐组合”与“已经授权” |

## 接受的代价

- Descriptor 仍需要平台域 registrar；它消除的是平台选择分支，不是假装所有 API 都同构。
- project Skill/Persona 只在工作区被会话授权后可见，同一项目在不同会话里可能得到不同目录。
- Persona 工具 pattern 是轻量筛选，不是权限语言；真正安全边界仍要看 capability 与审批。
- 切换 Persona 会把它的默认审批档写入会话，因此客户端必须明确提示；普通 Provider/模型更新
  不得重新套默认值。

## 后续影响

- 下一批企微/钉钉消息域通过 Descriptor registrar 接入。
- 深度研究、会议复盘、周报编制新增 Capability，不修改 Cowork tool loop。
- Skill 页面通过 `conversation_id` 展示项目层；Persona 目录同样按已授权根目录合并。
