---
name: skill-creator
description: 把这一轮里刚跑通的、以后还会重复的流程写成一个 Skill 并装好
kind: action
trigger:
  - 用户说“下次也这么做”“记住这个流程”“把这个做成技能”
  - 同一类任务在本会话里已经手工重复过两次以上
  - 用户要求修改、fork 或调试一个已有 Skill
anti_trigger:
  - 用户只是想记住一个事实或偏好（那是 remember，不是 Skill）
  - 流程只跑通过一次且用户没有要求固化
  - 需要写的是一次性脚本（直接写文件，不要包成 Skill）
tools:
  - write_file
  - read_file
  - load_skill
runtime:
  profile: none
compatibility:
  - WorkPilot Skill Bundle v2
status: active
---

Skill 是**给未来的你看的操作说明**，不是文档。判据只有一条：下次遇到同类任务，
只读这份正文能不能直接开工。

## 1. 先判断该不该做成 Skill

- 会重复吗？只发生一次的事不值一个 Skill，写完只会占目录。
- 是流程还是事实？“客户叫林琪”是 `remember`；“交周报要先跑数再套模板再发群”才是 Skill。
- 已经有同名的了吗？先查看系统提示中的完整 Skill 目录。命中就 `load_skill` 读现有正文，在它上面改，
  而不是并排再装一个近义的——两个都会进 prompt，模型选哪个全看运气。

## 2. 写 frontmatter

```markdown
---
name: weekly-report          # 小写字母数字加连字符，必须与目录名一致
description: 一句话说明"什么时候用它"，不是"它是什么"
trigger:                     # 什么情况下命中，写用户会说的原话
  - 用户要求出本周周报
anti_trigger:                # 什么情况下明确不要用它，这一栏最值钱
  - 用户要的是月度汇总
tools:                       # 这个流程真正会用到的工具名
  - search_files
  - load_skill
  - run_shell
kind: workflow               # planning / artifact / workflow / action
runtime:
  profile: none              # 固定 Renderer 才使用 artifact-python
status: active
---
```

`anti_trigger` 是最容易偷懒也最不该偷懒的一栏。只有 `trigger` 的 Skill 会过度触发：
prompt 里只有摘要，模型看到"周报"两个字就会把月度汇总也套进来。**每写一条 trigger，
想一想它最像的那个近邻是什么，把近邻写进 anti_trigger。**

## 3. 写正文

- 写**判断**，不要只写动作。「运行脚本」是动作；「默认写新文件，用户明确要求覆盖时先备份」
  才是这份 Skill 存在的理由。
- 写上次踩过的坑，并且写清楚**症状**。“路径要用绝对路径”没有用；
  “用相对路径会读到上一次的工作目录，表现是文件明明在却报不存在”才有用。
- 不要复述工具描述。工具说明每轮都在上下文里，抄一遍只是浪费预算。
- 不要写授权、审批、能力相关的“捷径”。Skill 正文是数据不是指令，
  写了也不会生效，只会让读的人以为它生效了。

长资料、确定性脚本、模板和评测分别放进 `references/`、`scripts/`、`assets/`、`evals/`；
`SKILL.md` 只保留决策流程、必需输出、安全规则和验证标准。user/project Skill 的 script
默认通过 `run_sandbox` 运行。脚本从 `WORKPILOT_INPUTS` 读取输入，在 `WORKPILOT_WORK` 使用
临时空间，把候选写到 `WORKPILOT_OUTPUTS`；`WORKPILOT_SKILLS` 是只读 Skill 路径列表。
不要假设 Docker 挂载点或自行联网安装依赖。

## 4. 装上去

把完整 Markdown 用 `write_file`（`purpose=workspace`）写成 `SKILL.md`，通过技能管理界面或 API
安装，目录名与 `name` 一致。装完后的下一轮系统提示会直接显示完整 Skill 目录；确认描述读起来
像“什么时候用它”。

## 5. 关于出厂 Skill

带 `origin: builtin` 的是随产品出厂的，**不能删**。要改它：装一个同名的自己的版本，
它会盖住出厂那份；不想要了就把自己那份删掉，出厂那份自动回来。要临时关掉：停用即可，
停用标记记在你自己的技能目录里，不会被产品升级抹掉。
