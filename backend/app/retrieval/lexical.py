import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.retrieval.dense import DenseSearchHit


async def lexical_search(
    session: AsyncSession,
    *,
    query: str,
    top_k: int = 50,
) -> list[DenseSearchHit]:
    if not query.strip():
        raise ValueError("query 不能为空")
    if not 1 <= top_k <= 50:
        raise ValueError("top_k 必须位于 1 到 50")
    terms = lexical_terms(query)
    if not terms:
        return []
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
                        matches.matched::float / :term_count AS score,
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
                    CROSS JOIN LATERAL (
                        SELECT count(*) AS matched
                        FROM unnest(CAST(:terms AS text[])) AS term
                        WHERE position(
                            term IN lower(
                                d.title || ' ' ||
                                array_to_string(COALESCE(c.heading_path, ARRAY[]::text[]), ' ') ||
                                ' ' || c.content
                            )
                        ) > 0
                    ) matches
                    WHERE c.is_searchable=true
                      AND c.strategy='heading'
                      AND d.deleted_at IS NULL
                      AND v.invalid_at IS NULL
                      AND matches.matched > 0
                    ORDER BY matches.matched DESC, length(c.content), c.id
                    LIMIT :top_k
                    """
                ),
                {"terms": terms, "term_count": len(terms), "top_k": top_k},
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
            lexical_score=float(row["score"]),
            heading_path=list(row["heading_path"]),
            blocks=list(row["blocks"]),
        )
        for row in rows
    ]


def lexical_terms(query: str, *, max_terms: int = 24) -> list[str]:
    normalized = query.casefold()
    latin = re.findall(r"[a-z0-9]+(?:[._+/-][a-z0-9]+)*", normalized)
    chinese_runs = re.findall(r"[\u4e00-\u9fff]+", normalized)
    chinese: list[str] = []
    for run in chinese_runs:
        if len(run) <= 4:
            chinese.append(run)
        chinese.extend(run[index : index + 2] for index in range(len(run) - 1))
    terms = [item for item in [*latin, *chinese] if len(item) >= 2]
    return list(dict.fromkeys(terms))[:max_terms]
