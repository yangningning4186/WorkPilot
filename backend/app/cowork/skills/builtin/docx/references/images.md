# Images and SVG

用 image block 放置已授权的本地 SVG/PNG/JPEG/GIF：

- `image_path`：相对输出目录或绝对本地路径；
- `image_alt`：准确说明图片传递的信息，纯装饰图可用空字符串；
- `image_caption`：需要可见的图号、来源或说明时填写；
- `image_width_inches`：1–6.5 英寸，未指定时使用版心内的默认宽度。

流程、架构和示意图优先用 SVG，因为源文件可继续编辑。SVG 必须有 `viewBox`，不得包含脚本、
事件属性、`foreignObject`、外部 `href`、远程字体或嵌入媒体。Renderer 会先安全校验，再转换为
兼容 PNG 嵌入 DOCX；不要把远程 URL 直接写入文档关系。

照片或插画只来自用户提供的素材，或当前环境中确实可用的图片生成工具。没有合适素材时保持清晰
排版，不虚构图片来源。生成后检查比例、清晰度、caption 与分页，避免图片独占一页而标题落在前页。
