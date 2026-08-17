import re
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.retrieval.dense import DenseSearchHit
from app.retrieval.strategy import ChunkStrategy, validate_chunk_strategy

# 词法打分方式。coverage 是"命中词数 / 总词数", 无停用词表、无 IDF、子串匹配,
# 英文自然语言查询下会被 the/is/in/how 这类词灌满(实测 in 命中 85% 的 chunk),
# 判别词反而进不了 Top-10。ts_rank / ts_rank_cd 走 PG 分语言 tsvector,
# 见迁移 0009 与台账 E2。
# 第一项是当前默认值; 换默认要先有配对评测。
LEXICAL_MODES = ("ts_rank", "coverage", "ts_rank_cd")

# 溯源元数据: block_id / locations 是产品硬约束, 两种打分方式共用同一段投影。
_BLOCKS_SQL = """
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
"""

_COLUMNS_SQL = """
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
    COALESCE(c.heading_path, ARRAY[]::text[]) AS heading_path,
"""

_COVERAGE_SQL = f"""
    SELECT
        {_COLUMNS_SQL}
        matches.matched::float / :term_count AS score,
        {_BLOCKS_SQL}
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
    WHERE __VISIBILITY__
      AND c.strategy='__CHUNK_STRATEGY__'
      AND d.deleted_at IS NULL
      AND matches.matched > 0
    ORDER BY matches.matched DESC, length(c.content), c.id
    LIMIT :top_k
"""

# 查询侧 tsquery 用 OR 而不是 AND: plainto_tsquery / websearch_to_tsquery 都是
# 词间 AND, 换上去等于要求 chunk 命中查询里每一个词, 召回会塌。这里把查询过一遍
# 与列同配置的 to_tsvector(顺带拿到停用词过滤和词干还原), 再把 lexeme 用 | 串起来。
# 命中多少、权重多高交给排序函数去区分。
# 过滤含引号/反斜杠的 lexeme: quote_literal 对它们会产出 to_tsquery 解析不了的形式。
_OR_TSQUERY_SQL = """
    to_tsquery('simple', (
        SELECT string_agg(quote_literal(lexeme), ' | ')
        FROM unnest(to_tsvector('{config}', {fn}(:query)))
        WHERE lexeme !~ '[''\\\\]'
    ))
"""

# {rank} 取 ts_rank 或 ts_rank_cd。两者都留着是因为 cover density 对中文 bigram
# 明确有害(台账 E2): 一句中文自然语言查询会切出十几个 bigram, 其中「需要/哪些/掌握」
# 这类到处都有, cover density 会在任意文档里找到它们的紧凑覆盖并给高分, 反而惩罚
# 主题 bigram 分散在整个 chunk 里的正确结果; ts_rank 按词频累加, 正合中文的需要。
#
# 权重数组按 {{D,C,B,A}} 排列, 平权即与覆盖率打分同口径(单变量)。
# 归一化 32 即 rank/(rank+1), 把两种语言的分数都压到 [0,1) 才能相加 ——
# 两列词表不同, 原始分数不可比。
_RANKED_SQL_TEMPLATE = f"""
    WITH q AS (
        SELECT
            {_OR_TSQUERY_SQL.format(config="english", fn="lexical_en_text")} AS q_en,
            {_OR_TSQUERY_SQL.format(config="simple", fn="lexical_zh_bigrams")} AS q_zh
    )
    SELECT
        {_COLUMNS_SQL}
        COALESCE({{rank}}('{{{{1,1,1,1}}}}', c.tsv_en, q.q_en, 32), 0)
      + COALESCE({{rank}}('{{{{1,1,1,1}}}}', c.tsv_zh, q.q_zh, 32), 0) AS score,
        {_BLOCKS_SQL}
    FROM chunks c
    JOIN document_versions v ON v.id=c.version_id
    JOIN documents d ON d.id=v.document_id
    CROSS JOIN q
    WHERE __VISIBILITY__
      AND c.strategy='__CHUNK_STRATEGY__'
      AND d.deleted_at IS NULL
      AND (
            (q.q_en IS NOT NULL AND c.tsv_en @@ q.q_en)
         OR (q.q_zh IS NOT NULL AND c.tsv_zh @@ q.q_zh)
      )
    ORDER BY score DESC, length(c.content), c.id
    LIMIT :top_k
"""


async def lexical_search(
    session: AsyncSession,
    *,
    query: str,
    top_k: int = 50,
    mode: str = LEXICAL_MODES[0],
    strategy: ChunkStrategy = "heading",
    temporal_ctx: datetime | None = None,
) -> list[DenseSearchHit]:
    if not query.strip():
        raise ValueError("query 不能为空")
    if not 1 <= top_k <= 100:
        raise ValueError("top_k 必须位于 1 到 100")
    if mode not in LEXICAL_MODES:
        raise ValueError(f"未知的 lexical_mode: {mode}, 可选 {LEXICAL_MODES}")
    strategy = validate_chunk_strategy(strategy)

    if mode == "coverage":
        terms = lexical_terms(query)
        if not terms:
            return []
        sql = _COVERAGE_SQL.replace("__CHUNK_STRATEGY__", strategy)
        params: dict[str, object] = {
            "terms": terms,
            "term_count": len(terms),
            "top_k": top_k,
        }
    else:
        sql = _RANKED_SQL_TEMPLATE.format(rank=mode).replace("__CHUNK_STRATEGY__", strategy)
        params = {"query": query, "top_k": top_k}

    visibility = (
        "c.is_searchable=true AND v.invalid_at IS NULL"
        if temporal_ctx is None
        else (
            "v.activated_at IS NOT NULL "
            "AND v.activated_at <= :temporal_ctx "
            "AND (v.invalid_at IS NULL OR v.invalid_at > :temporal_ctx)"
        )
    )
    sql = sql.replace("__VISIBILITY__", visibility)
    params["temporal_ctx"] = temporal_ctx

    rows = (await session.execute(text(sql), params)).mappings().all()
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
            strategy=strategy,
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
