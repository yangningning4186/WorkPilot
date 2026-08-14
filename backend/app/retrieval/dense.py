from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm.gateway import ModelGateway


async def apply_hnsw_scan_settings(session: AsyncSession) -> None:
    """按 docs/03 §4.1 设置本事务的 pgvector 扫描参数。

    部分索引只覆盖 strategy 与 is_searchable, embedding 身份等条件仍在索引内过滤;
    没有迭代扫描时这些过滤会在候选阶段丢行, 结果可能凑不满 top-k。

    用 SET LOCAL 而不是 SET: 连接是池化的, 会话级设置会漏给后续无关查询。
    """

    settings = get_settings()
    # SET 不接受绑定参数; 取值来自 Literal 与带边界的 int, 不存在注入面。
    await session.execute(text(f"SET LOCAL hnsw.iterative_scan = {settings.hnsw_iterative_scan}"))
    await session.execute(
        text(f"SET LOCAL hnsw.max_scan_tuples = {settings.hnsw_max_scan_tuples:d}")
    )
    await session.execute(text(f"SET LOCAL hnsw.ef_search = {settings.hnsw_ef_search:d}"))


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
    rerank_score: float | None = None
    dense_score: float | None = None
    lexical_score: float | None = None
    fusion_score: float | None = None


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
    return await _dense_search_by_vector(
        session,
        gateway,
        embedding=result.embeddings[0],
        top_k=top_k,
    )


async def multi_query_dense_search(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    queries: list[str],
    top_k: int = 10,
    per_query_top_k: int = 50,
) -> list[DenseSearchHit]:
    normalized = list(dict.fromkeys(" ".join(query.split()) for query in queries if query.strip()))
    if not normalized:
        raise ValueError("queries 不能为空")
    if not 1 <= top_k <= 50:
        raise ValueError("top_k 必须位于 1 到 50")
    if not 1 <= per_query_top_k <= 50:
        raise ValueError("per_query_top_k 必须位于 1 到 50")
    if len(normalized) == 1:
        return await dense_search(session, gateway, query=normalized[0], top_k=top_k)

    result = await gateway.embed(normalized, task_type="multi_query_embedding")
    rankings = [
        await _dense_search_by_vector(
            session,
            gateway,
            embedding=embedding,
            top_k=per_query_top_k,
        )
        for embedding in result.embeddings
    ]
    return _merge_query_rankings(rankings, top_k=top_k)


def _merge_query_rankings(
    rankings: list[list[DenseSearchHit]], *, top_k: int
) -> list[DenseSearchHit]:
    # 多跳查询需要覆盖互补证据。RRF 会过度奖励在多个 query 中重复出现的同一主题 chunk,
    # 反而挤掉只被某个子查询命中的第二跳证据; 因此按 query 轮询头部结果并去重。
    best_hits: dict[UUID, DenseSearchHit] = {}
    for ranking in rankings:
        for hit in ranking:
            current = best_hits.get(hit.chunk_id)
            if current is None or hit.score > current.score:
                best_hits[hit.chunk_id] = hit
    merged: list[DenseSearchHit] = []
    seen: set[UUID] = set()
    max_rank = max((len(ranking) for ranking in rankings), default=0)
    for rank in range(max_rank):
        for ranking in rankings:
            if rank >= len(ranking):
                continue
            hit = ranking[rank]
            if hit.chunk_id in seen:
                continue
            seen.add(hit.chunk_id)
            merged.append(best_hits[hit.chunk_id])
            if len(merged) >= top_k:
                return merged
    return merged


async def _dense_search_by_vector(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    embedding: list[float],
    top_k: int,
) -> list[DenseSearchHit]:
    await apply_hnsw_scan_settings(session)
    vector = "[" + ",".join(format(value, ".9g") for value in embedding) + "]"
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
            dense_score=float(row["score"]),
            heading_path=list(row["heading_path"]),
            blocks=list(row["blocks"]),
        )
        for row in rows
    ]
