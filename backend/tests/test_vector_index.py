"""部分 HNSW 索引的命中与召回验证(docs/03 §4.1)。

这里验的不是"快不快", 而是两件会让 W3 四策略对照结论作废的事:
索引是否真的被用上, 以及索引内过滤会不会把候选丢到凑不满 top-k。
"""

import random

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.retrieval.dense import apply_hnsw_scan_settings

pytestmark = pytest.mark.integration

DIMENSIONS = 1024
# 够让规划器认真考虑索引, 又不至于把测试跑成分钟级。
ROWS_PER_STRATEGY = 400


def _vector(rng: random.Random) -> list[float]:
    values = [rng.gauss(0.0, 1.0) for _ in range(DIMENSIONS)]
    norm = sum(value * value for value in values) ** 0.5 or 1.0
    return [value / norm for value in values]


def _literal(vector: list[float]) -> str:
    return "[" + ",".join(format(value, ".9g") for value in vector) + "]"


async def _seed(session: AsyncSession, rng: random.Random) -> None:
    source_id, document_id, version_id = uuid7(), uuid7(), uuid7()
    await session.execute(
        text(
            "INSERT INTO sources (id, kind, name, config) "
            "VALUES (:id, 'local_dir', 'vec', '{}'::jsonb)"
        ),
        {"id": source_id},
    )
    await session.execute(
        text(
            "INSERT INTO documents (id, source_id, source_uri, title, doc_type) "
            "VALUES (:id, :source_id, 'vec://index', 'vec', 'note')"
        ),
        {"id": document_id, "source_id": source_id},
    )
    await session.execute(
        text(
            "INSERT INTO document_versions "
            "(id, document_id, version_no, content_hash, parser, parser_version, "
            " full_text, parse_status, activated_at, "
            " embedding_model, embedding_provider, embedding_revision) "
            "VALUES (:id, :document_id, 1, 'hash', 'test', '1', "
            " '', 'done', now(), "
            " 'fake-embedding', 'deterministic_test', 'rev-1')"
        ),
        {"id": version_id, "document_id": document_id},
    )

    rows = []
    index = 0
    for strategy in ("heading", "fixed"):
        for position in range(ROWS_PER_STRATEGY):
            rows.append(
                {
                    "id": uuid7(),
                    "version_id": version_id,
                    "strategy": strategy,
                    "chunk_index": position,
                    "embedding": _literal(_vector(rng)),
                    # 留一部分不可检索, 验证部分索引谓词确实生效。
                    "is_searchable": position % 10 != 0,
                    "index": index,
                }
            )
            index += 1
    await session.execute(
        text(
            """
            INSERT INTO chunks
                (id, version_id, strategy, chunk_index, content, content_tokens,
                 block_start_idx, block_end_idx, char_start, char_end,
                 dominant_block_type, embedding, doc_type, is_searchable,
                 embedding_model, embedding_provider, embedding_revision)
            VALUES
                (:id, :version_id, :strategy, :chunk_index, 'c', 1,
                 0, 0, :index, :index + 1,
                 'paragraph', CAST(:embedding AS vector), 'note', :is_searchable,
                 'fake-embedding', 'deterministic_test', 'rev-1')
            """
        ),
        rows,
    )
    await session.execute(text("ANALYZE chunks"))


async def test_partial_index_is_used_and_scoped_to_its_strategy(
    db_session: AsyncSession,
) -> None:
    rng = random.Random(20260814)
    await _seed(db_session, rng)
    probe = _literal(_vector(rng))

    await apply_hnsw_scan_settings(db_session)
    # 这点数据量下顺扫本来就更便宜, 规划器选它是对的; 这里要验证的不是"会不会用",
    # 而是"能不能用"——谓词是否覆盖得住查询。把顺扫、位图和排序都关掉之后,
    # 唯一能满足 ORDER BY 的就只剩向量索引, 它选谁就说明谁真的适用。
    await db_session.execute(text("SET LOCAL enable_seqscan = off"))
    await db_session.execute(text("SET LOCAL enable_bitmapscan = off"))
    await db_session.execute(text("SET LOCAL enable_sort = off"))
    plan = "\n".join(
        line
        for line in (
            await db_session.execute(
                text(
                    """
                    EXPLAIN
                    SELECT c.id FROM chunks c
                    WHERE c.strategy = 'heading' AND c.is_searchable
                    ORDER BY c.embedding <=> CAST(:embedding AS vector)
                    LIMIT 10
                    """
                ),
                {"embedding": probe},
            )
        )
        .scalars()
        .all()
        if "Scan" in line
    )

    # 命中的必须是 heading 自己的部分索引; 四策略共用一个索引正是 P1-c 那个错误。
    assert "idx_chunk_vec_heading" in plan, plan
    assert "idx_chunk_vec_fixed" not in plan, plan
    # 走索引就不该再出现顺扫, 否则说明谓词没覆盖住查询条件。
    assert "Seq Scan" not in plan, plan


async def test_index_scan_returns_the_same_top_k_as_exact_search(
    db_session: AsyncSession,
) -> None:
    """索引内过滤丢候选是静默的: 结果照样返回, 只是少了本该召回的行。"""

    rng = random.Random(20260815)
    await _seed(db_session, rng)
    probe = _literal(_vector(rng))
    query = """
        SELECT c.id FROM chunks c
        WHERE c.strategy = 'heading'
          AND c.is_searchable
          AND c.embedding IS NOT NULL
          AND c.embedding_model = 'fake-embedding'
          AND c.embedding_revision = 'rev-1'
        ORDER BY c.embedding <=> CAST(:embedding AS vector), c.id
        LIMIT 20
    """

    await apply_hnsw_scan_settings(db_session)
    # 必须真的把查询逼进向量索引: 只关顺扫的话规划器会退回 btree + 排序,
    # 那等于拿精确检索和精确检索比, 这个测试就什么都没验证。
    await db_session.execute(text("SET LOCAL enable_seqscan = off"))
    await db_session.execute(text("SET LOCAL enable_bitmapscan = off"))
    await db_session.execute(text("SET LOCAL enable_sort = off"))
    approximate = list(
        (await db_session.execute(text(query), {"embedding": probe})).scalars().all()
    )

    await db_session.execute(text("SET LOCAL enable_seqscan = on"))
    await db_session.execute(text("SET LOCAL enable_sort = on"))
    await db_session.execute(text("SET LOCAL enable_indexscan = off"))
    await db_session.execute(text("SET LOCAL enable_bitmapscan = off"))
    exact = list((await db_session.execute(text(query), {"embedding": probe})).scalars().all())

    assert len(approximate) == 20, "候选不足说明迭代扫描没兜住索引内过滤"
    overlap = len(set(approximate) & set(exact))
    assert overlap >= 19, f"近邻召回相对精确检索掉了 {20 - overlap} 条"


async def test_unsearchable_chunks_never_surface(db_session: AsyncSession) -> None:
    rng = random.Random(20260816)
    await _seed(db_session, rng)
    probe = _literal(_vector(rng))

    await apply_hnsw_scan_settings(db_session)
    leaked = (
        await db_session.execute(
            text(
                """
                SELECT count(*) FROM (
                    SELECT c.is_searchable FROM chunks c
                    WHERE c.strategy = 'heading' AND c.is_searchable
                    ORDER BY c.embedding <=> CAST(:embedding AS vector)
                    LIMIT 50
                ) AS hits
                WHERE NOT hits.is_searchable
                """
            ),
            {"embedding": probe},
        )
    ).scalar_one()
    assert leaked == 0


async def test_scan_settings_are_transaction_local(db_session: AsyncSession) -> None:
    """SET LOCAL 而不是 SET: 连接池化, 会话级设置会漏给后续无关查询。"""

    await apply_hnsw_scan_settings(db_session)
    inside = (await db_session.execute(text("SHOW hnsw.ef_search"))).scalar_one()
    assert inside == "100"

    await db_session.rollback()
    after = (await db_session.execute(text("SHOW hnsw.ef_search"))).scalar_one()
    assert after != "100"
