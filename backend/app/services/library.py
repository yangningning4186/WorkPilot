"""资料库页的读模型。

只读、聚合、给人看。刻意不复用 annotation 的文档列表: 那个是标注工具用的,
按 block 数排版、强制 admin; 这里要的是"每篇现在处于什么状态", 包括
**候选版本解析失败但旧版仍在服务** 这种约束 10 才有的中间态。

不暴露 sources.config: 那里面是本地绝对路径, 属于 owner 环境信息, 没有理由
给到浏览器（哪怕单用户, 演示时也会截图）。
"""

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.library import (
    LibraryDocument,
    LibraryResponse,
    LibrarySource,
    LibraryTotals,
)

# 一次查完: 活跃版本的解析产物、chunk 计数、是否有 bbox 定位、以及候选版本的状态。
# 分成 N 次查询会在文档变多时变成 N+1, 而这个页面就是给几百篇资料用的。
_DOCUMENTS_SQL = """
WITH active AS (
    SELECT DISTINCT ON (v.document_id)
           v.document_id, v.id AS version_id, v.version_no, v.parser,
           v.page_count, v.parse_status, v.updated_at
    FROM document_versions v
    WHERE v.activated_at IS NOT NULL AND v.invalid_at IS NULL
    ORDER BY v.document_id, v.version_no DESC
),
candidate AS (
    SELECT DISTINCT ON (v.document_id)
           v.document_id, v.parse_status, v.parse_error, v.updated_at
    FROM document_versions v
    WHERE v.activated_at IS NULL AND v.invalid_at IS NULL
    ORDER BY v.document_id, v.version_no DESC
),
block_stats AS (
    SELECT b.version_id,
           count(*) AS block_count,
           bool_or(l.block_id IS NOT NULL) AS locatable
    FROM parsed_blocks b
    LEFT JOIN parsed_block_locations l ON l.block_id = b.id
    GROUP BY b.version_id
),
chunk_stats AS (
    SELECT c.version_id,
           count(*) AS chunk_count,
           count(*) FILTER (WHERE c.is_searchable) AS searchable_chunk_count
    FROM chunks c
    GROUP BY c.version_id
)
SELECT d.id AS document_id, d.title, d.source_uri, d.doc_type,
       s.name AS source_name, s.kind AS source_kind,
       a.version_id, a.version_no, a.parser, a.page_count,
       a.parse_status AS active_parse_status,
       cand.parse_status AS candidate_parse_status,
       cand.parse_error AS candidate_parse_error,
       COALESCE(bs.block_count, 0) AS block_count,
       COALESCE(bs.locatable, false) AS locatable,
       COALESCE(cs.chunk_count, 0) AS chunk_count,
       COALESCE(cs.searchable_chunk_count, 0) AS searchable_chunk_count,
       GREATEST(d.updated_at, COALESCE(a.updated_at, d.updated_at),
                COALESCE(cand.updated_at, d.updated_at)) AS updated_at
FROM documents d
JOIN sources s ON s.id = d.source_id
LEFT JOIN active a ON a.document_id = d.id
LEFT JOIN candidate cand ON cand.document_id = d.id
LEFT JOIN block_stats bs ON bs.version_id = a.version_id
LEFT JOIN chunk_stats cs ON cs.version_id = a.version_id
WHERE d.deleted_at IS NULL
  AND (:query = '' OR d.title ILIKE '%' || :query || '%'
       OR d.source_uri ILIKE '%' || :query || '%')
ORDER BY updated_at DESC, d.title
LIMIT :limit
"""

_SOURCES_SQL = """
SELECT s.id, s.name, s.kind, s.sync_status, s.sync_error, s.last_sync_at,
       count(d.id) FILTER (WHERE d.deleted_at IS NULL) AS document_count
FROM sources s
LEFT JOIN documents d ON d.source_id = s.id
GROUP BY s.id
ORDER BY s.name
"""


def _document_state(row: dict[str, Any]) -> str:
    """把版本状态翻译成产品语义。

    顺序有讲究: 候选版本失败要盖过"已激活且正常", 否则用户看到一片 ready,
    却不知道最新一版根本没进去(约束 10 说的正是这种沉默降级)。
    """

    candidate_status = row["candidate_parse_status"]
    if candidate_status in {"pending", "parsing"}:
        return "parsing"
    if candidate_status == "failed":
        return "failed"
    if row["version_id"] is None:
        return "parsing" if candidate_status is not None else "failed"
    if row["searchable_chunk_count"] == 0:
        return "stale"
    return "ready"


async def get_library_overview(
    session: AsyncSession, *, query: str = "", limit: int = 500
) -> LibraryResponse:
    rows = (
        (await session.execute(text(_DOCUMENTS_SQL), {"query": query.strip(), "limit": limit}))
        .mappings()
        .all()
    )
    source_rows = (await session.execute(text(_SOURCES_SQL))).mappings().all()
    await session.rollback()

    documents = [
        LibraryDocument(
            document_id=row["document_id"],
            version_id=row["version_id"],
            title=row["title"],
            source_uri=row["source_uri"],
            doc_type=row["doc_type"],
            source_name=row["source_name"],
            source_kind=row["source_kind"],
            state=_document_state(dict(row)),
            parser=row["parser"],
            parse_error=row["candidate_parse_error"],
            page_count=row["page_count"],
            block_count=row["block_count"],
            chunk_count=row["chunk_count"],
            searchable_chunk_count=row["searchable_chunk_count"],
            locatable=row["locatable"],
            version_no=row["version_no"],
            updated_at=row["updated_at"],
        )
        for row in rows
    ]
    return LibraryResponse(
        sources=[
            LibrarySource(
                id=row["id"],
                name=row["name"],
                kind=row["kind"],
                sync_status=row["sync_status"],
                sync_error=row["sync_error"],
                document_count=row["document_count"],
                last_sync_at=row["last_sync_at"],
            )
            for row in source_rows
        ],
        documents=documents,
        totals=_totals(documents),
    )


def _totals(documents: list[LibraryDocument]) -> LibraryTotals:
    return LibraryTotals(
        documents=len(documents),
        chunks=sum(item.chunk_count for item in documents),
        searchable_chunks=sum(item.searchable_chunk_count for item in documents),
        parsing=sum(1 for item in documents if item.state == "parsing"),
        failed=sum(1 for item in documents if item.state == "failed"),
    )
