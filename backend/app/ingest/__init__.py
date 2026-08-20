"""文档解析与分块。

**两个产品共用**：RAG 用它把 PDF/Markdown 解析入库，Cowork 的 `read_local_pdf`
工具直接调 `parse_pdf` 读用户目录里的文件。因此本包留在 `app/` 顶层而不是
`app/rag/` 之下（ADR-0011 Step 3）。

包内 `chunking` / `chunk_strategies` 目前只有 RAG 用，是候选的进一步下沉点。
"""
