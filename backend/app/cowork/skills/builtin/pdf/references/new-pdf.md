# New PDF

使用 `PdfSpec` 的 summary、section 和 paragraph/bullets/table/quote/callout block 表达内容。
固定 Renderer 采用 A4 安全边距和真正的多页流式排版；表格行避免跨页断裂。内容过长时自然分页，
不能缩小整份文档塞进单页。生成后 Validator 会逐页栅格化、检查空白页和文本边界。

PDF 是固定阅读交付物：需要继续计算、筛选的大表改用 XLSX；需要继续编辑长文改用 DOCX。PDF 中
只放阅读所需的摘要与关键表。每节一个主题，事实页绑定 section claim；没有来源的信息明确标为
未确认，不因“PDF 看起来正式”而补全。

写 Spec 前检查：section id 唯一、没有空 section、表格有表头且列数适合 A4、图片来自授权本地路径
并有 alt/caption。正文、列表、表格和图片字段不要混填；Schema 可能接受部分无效字段，但 Renderer
只消费与 block type 对应的字段。
