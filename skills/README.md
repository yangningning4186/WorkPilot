# WorkPilot Skills

每个启用的 Skill 放在独立目录中，入口必须命名为 `SKILL.md`，目录名与
frontmatter 的 `name` 保持一致：

```text
skills/
  summarize-contract/
    SKILL.md
```

最小格式：

```markdown
---
name: summarize-contract
description: 提取合同的主体、期限、金额、违约责任与风险点
trigger:
  - 用户要求审阅或总结合同
anti_trigger:
  - 用户要求提供正式法律意见
tools:
  - read_text_file
status: active
---

1. 先读取原文并确认合同主体。
2. 按约定字段输出，缺失内容明确标记为“未找到”。
```

运行时只注入摘要；完整正文由 Agent 命中条件后通过 `load_skill` 渐进加载。
