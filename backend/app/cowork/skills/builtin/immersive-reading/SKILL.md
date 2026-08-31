---
name: immersive-reading
description: 带着用户读一份具体文档时，如何取证、引用并驱动阅读器翻页高亮
kind: workflow
trigger:
  - 用户就某一份 PDF、论文、合同或长文档提问
  - 用户问“这篇里怎么说的”“第几页写了什么”“帮我读一下”
  - 回答需要引用某份文档的原文
anti_trigger:
  - 问题跨多份文档、需要先检索资料库（那是 search_knowledge）
  - 用户要的是改写或生成文档，不是读它
  - 只需要文件的字面内容，不需要引用与定位（用 read_file 更直接）
tools:
  - material_outline
  - search_material
  - read_material
  - reader_goto
  - reader_annotate
runtime:
  profile: none
compatibility:
  - Evidence Ledger v1
status: active
---

用户屏幕上有两样东西：你的回答，和旁边那份文档。**你的工作是让这两样对得上。**

## 取证顺序

1. `material_outline` 看结构——单位数、大纲、每项的 locator。想知道"方法在哪一节"，
   这一次调用就够了，比一页页读便宜一个量级。
2. `search_material` 定位——拿术语、人名、公式或原句找 locator。
3. `read_material` 读原文——按 locator 取。**你关于这份文档的每一个论断都必须来自这一步。**

## 三条硬规矩

**没读过就是不知道。** `search_material` 返回的是开窗片段，可能从句子中间断开。
片段里出现了某个词，不代表你知道那句话在说什么。要下判断，先 `read_material` 读全。

**每个论断都带 locator。** 每一只读工具的返回都自带 locator，所以"这句话出自第 12 页"
是取证的副产品，不是事后要补的一句话。回答里用 `[p.12]` 这样的标记，用户可以点它跳转。

**讲到哪就翻到哪。** 每讨论到一处具体内容就调一次 `reader_goto(path, locator, quote)`，
用户于是能边看你的回答边看到依据。不要攒到最后只翻一次——中间那几处等于没有出处。

**批注只有用户明确要求才写。** `reader_annotate(path, locator, quote, note)` 会把高亮和备注
持久保存到磁盘，不是普通翻页的替代品。它的 `quote` 必须逐字命中原文；对不上就停止并重新
`read_material`，不要把译文或凭记忆改写的句子硬塞进去。批注成功后仍要在回答里解释其意义。

## quote 怎么给

`quote` 必须是原文里**逐字**出现的文字，从 `read_material` 的返回里复制，不要重新打一遍。
对不上时阅读器仍会翻页，只是不高亮。

用中文讨论英文文献时，你写的"引文"是你自己的翻译，永远不可能逐字命中原文。
这种情况下：**quote 传原文那一句英文，正文里写你的翻译。** 高亮落在原文上，
翻译留在回答里，两边都成立。

## 常见错法

- 用 `read_file` 读 PDF 再谈页码：那条路没有 locator，你报的页码是猜的。
- 一次 `read_material` 拉走整份文档：预算烧完了，而且回答会退化成泛泛的总结。
  先 outline 再定位再读，是省钱也是让回答具体。
- 引用一段 `search_material` 的片段：片段边界不是句子边界，你可能正在引用半句话。
