---
name: html-report
description: 用 HtmlReportSpec 生成离线、单文件、无外部依赖的可审计 HTML 报告
metadata:
  kind: artifact
  trigger:
    - 用户要求 HTML 数据报告、研究报告或审计报告
    - 报告需要在浏览器中离线预览
  anti_trigger:
    - 用户要求交互式应用、dashboard 或多页面网站
    - 用户要求飞书云文档
  tools:
    - load_skill
    - load_skill_resource
    - render_artifact
  runtime:
    profile: artifact-python
  compatibility:
    - offline HTML
  status: active
---

# Goal

生成单一、离线、无脚本和无远程依赖的 HTML 报告。交互应用属于 web-app，不在本 Skill 范围。

# Workflow

1. 若本轮尚无 Office Brief/source map，先 `load_skill("office-workflow")`；已由它交接时不重复加载。
2. 按阅读任务构造 HtmlReportSpec：摘要、章节、段落、列表、表格与 claim 绑定；普通章节不得只有
   标题或一句占位式描述。
3. 调用 `render_artifact`；样式由 Renderer 内嵌，不提供任意 CSS/JS。
4. 检查内容闭环、离线可用性，以及无 script、iframe、object、事件处理器、远程 URL 和
   meta refresh 后再交付。

# Validation

要求结构、安全、证据通过；HTML 必须能在 sandboxed iframe 中离线打开。
