"""解析结果 → LlamaIndex `Document`，切分交给 `SentenceSplitter`。

**一份 PDF 切成「每页一个 Document」而不是「整篇一个」。** LlamaIndex 的切分器会把
Document 的 metadata 原样复制给它切出来的每个节点，所以按页喂进去，页码就自动跟着每个
片段走——这是让引用能落回"第几页"的最省事的办法，代价只是构造 Document 时多一层循环。
整篇喂进去的话，节点只知道自己属于哪个文件，不知道在哪一页。

比起自己按解析块聚合，这里换掉的是 **bbox 级精确高亮**：切分器按字符窗口切，块边界和
字符区间对不上了，所以证据只带页码，不带矩形框。PDF 预览因此只能翻到页，不能圈出那句话。
"""

from __future__ import annotations

from llama_index.core.schema import Document

from app.ingest.types import ParsedBlock, ParsedDocument

# 每个片段的目标 token 数与重叠。LlamaIndex 的默认值是 1024/200，对论文偏大——一个片段
# 跨了三个小节，检索命中之后还得让模型自己在里面找。512 更接近"一个论点"的尺度。
CHUNK_SIZE = 512
CHUNK_OVERLAP = 64

# 这些字段只用来溯源，不能进 embedding 文本，也不能进给 LLM 的文本：把文件名和页码混进
# 待编码文本会稀释语义向量。
PROVENANCE_KEYS = ("doc_id", "filename", "title", "page_no", "heading_path")


def build_documents(
    parsed: ParsedDocument,
    *,
    doc_id: str,
    filename: str,
    title: str,
) -> list[Document]:
    """有页码的按页切，没有的整篇一个。"""
    pages = _pages(parsed)
    if not pages:
        text = parsed.full_text.strip() or "\n\n".join(
            block.text for block in parsed.blocks if block.text.strip()
        )
        if not text.strip():
            return []
        return [_document(text, doc_id=doc_id, filename=filename, title=title, page_no=None)]

    return [
        _document(text, doc_id=doc_id, filename=filename, title=title, page_no=page_no)
        for page_no, text in sorted(pages.items())
        if text.strip()
    ]


def _pages(parsed: ParsedDocument) -> dict[int, str]:
    """按首个 location 的页码把块归页。跨页的块算在它开始的那一页。"""
    buckets: dict[int, list[str]] = {}
    for block in parsed.blocks:
        if not block.locations or not block.text.strip():
            continue
        buckets.setdefault(block.locations[0].page_no, []).append(block.text)
    return {page_no: "\n\n".join(texts) for page_no, texts in buckets.items()}


def _document(
    text: str,
    *,
    doc_id: str,
    filename: str,
    title: str,
    page_no: int | None,
) -> Document:
    metadata: dict[str, object] = {"doc_id": doc_id, "filename": filename, "title": title}
    if page_no is not None:
        metadata["page_no"] = page_no
    return Document(
        text=text,
        metadata=metadata,
        excluded_embed_metadata_keys=list(PROVENANCE_KEYS),
        excluded_llm_metadata_keys=list(PROVENANCE_KEYS),
    )


def block_char_count(blocks: list[ParsedBlock]) -> int:
    return sum(len(block.text) for block in blocks)


__all__ = [
    "CHUNK_OVERLAP",
    "CHUNK_SIZE",
    "PROVENANCE_KEYS",
    "block_char_count",
    "build_documents",
]
