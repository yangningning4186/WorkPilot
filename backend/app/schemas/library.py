from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

# 文档在资料库页里的状态。语义直接对应约束 10 的版本激活规则:
#   ready   已激活版本解析成功, 正常参与检索
#   parsing 有候选版本正在解析(旧版若存在仍在服务)
#   failed  候选版本解析失败——**这不代表检索不可用**, 旧版还在顶着
#   stale   有已激活版本, 但没有可检索 chunk(通常是 embedding 没跟上)
DocumentState = str


class LibraryDocument(BaseModel):
    document_id: UUID
    version_id: UUID | None
    title: str
    source_uri: str
    doc_type: str
    source_name: str
    source_kind: str
    source_editable: bool
    state: DocumentState
    parser: str | None
    parse_error: str | None
    page_count: int | None
    block_count: int
    chunk_count: int
    searchable_chunk_count: int
    # 有 bbox 定位才可能做原文高亮; 前端据此提示"这篇只能给文本引用"。
    locatable: bool
    version_no: int | None
    updated_at: datetime


class LibrarySource(BaseModel):
    id: UUID
    name: str
    kind: str
    sync_status: str
    sync_error: str | None
    document_count: int
    last_sync_at: datetime | None


class LibraryTotals(BaseModel):
    documents: int
    chunks: int
    searchable_chunks: int
    parsing: int
    failed: int


class LibraryResponse(BaseModel):
    sources: list[LibrarySource]
    documents: list[LibraryDocument]
    totals: LibraryTotals
