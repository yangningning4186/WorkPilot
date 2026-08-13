from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.gateway import ModelGateway


@dataclass(frozen=True)
class DenseSearchHit:
    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    version_no: int
    title: str
    source_uri: str
    content: str
    score: float
    heading_path: list[str]
    blocks: list[dict[str, Any]]
    content_tokens: int = 0
    char_start: int = 0
    char_end: int = 0


async def dense_search(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    query: str,
    top_k: int = 10,
) -> list[DenseSearchHit]:
    if not query.strip():
        raise ValueError("query 不能为空")
    if not 1 <= top_k <= 50:
        raise ValueError("top_k 必须位于 1 到 50")
    result = await gateway.embed([query], task_type="query_embedding")
    vector = "[" + ",".join(format(value, ".9g") for value in result.embeddings[0]) + "]"
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT
                        c.id AS chunk_id,
                        d.id AS document_id,
                        v.id AS version_id,
                        v.version_no,
                        d.title,
                        d.source_uri,
                        c.content,
                        c.content_tokens,
                        c.char_start,
                        c.char_end,
                        1 - (c.embedding <=> CAST(:embedding AS vector)) AS score,
                        COALESCE(c.heading_path, ARRAY[]::text[]) AS heading_path,
                        COALESCE(
                            (
                                SELECT jsonb_agg(
                                    jsonb_build_object(
                                        'block_id', b.id,
                                        'block_idx', b.block_idx,
                                        'block_type', b.block_type,
                                        'text', b.text,
                                        'char_start', b.char_start,
                                        'char_end', b.char_end,
                                        'heading_path', COALESCE(b.heading_path, ARRAY[]::text[]),
                                        'locations', COALESCE(
                                            (
                                                SELECT jsonb_agg(
                                                    jsonb_build_object(
                                                        'page_no', l.page_no,
                                                        'page_width', l.page_width,
                                                        'page_height', l.page_height,
                                                        'rotation', l.rotation,
                                                        'coord_origin', l.coord_origin,
                                                        'bbox_norm', l.bbox_norm
                                                    ) ORDER BY l.location_idx
                                                )
                                                FROM parsed_block_locations l
                                                WHERE l.block_id=b.id
                                            ),
                                            '[]'::jsonb
                                        )
                                    ) ORDER BY b.block_idx
                                )
                                FROM parsed_blocks b
                                WHERE b.version_id=c.version_id
                                  AND b.block_idx BETWEEN c.block_start_idx AND c.block_end_idx
                            ),
                            '[]'::jsonb
                        ) AS blocks
                    FROM chunks c
                    JOIN document_versions v ON v.id=c.version_id
                    JOIN documents d ON d.id=v.document_id
                    WHERE c.is_searchable=true
                      AND c.strategy='heading'
                      AND c.embedding IS NOT NULL
                      AND c.embedding_model=:embedding_model
                      AND c.embedding_provider=:embedding_provider
                      AND c.embedding_revision=:embedding_revision
                      AND d.deleted_at IS NULL
                      AND v.invalid_at IS NULL
                    ORDER BY c.embedding <=> CAST(:embedding AS vector), c.id
                    LIMIT :top_k
                    """
                ),
                {
                    "embedding": vector,
                    "embedding_model": gateway.embedding_model,
                    "embedding_provider": gateway.embedding_provider,
                    "embedding_revision": gateway.embedding_revision,
                    "top_k": top_k,
                },
            )
        )
        .mappings()
        .all()
    )
    return [
        DenseSearchHit(
            chunk_id=row["chunk_id"],
            document_id=row["document_id"],
            version_id=row["version_id"],
            version_no=row["version_no"],
            title=row["title"],
            source_uri=row["source_uri"],
            content=row["content"],
            content_tokens=row["content_tokens"],
            char_start=row["char_start"],
            char_end=row["char_end"],
            score=float(row["score"]),
            heading_path=list(row["heading_path"]),
            blocks=list(row["blocks"]),
        )
        for row in rows
    ]
