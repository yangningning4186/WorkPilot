"""升 human 的纪律: 漂移、失效 span、缺复核人一律拒绝, 且必须留痕。"""

import json
from pathlib import Path

import eval.import_handwritten_suite as importer
import eval.promote_candidates as promoter
import pytest
from eval.build_m1_candidate_suite import CandidateSuiteError
from eval.promote_candidates import promote
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from uuid6 import uuid7

from app.llm.gateway import ModelGateway
from app.services.markdown_ingestion import ingest_markdown_file
from tests.fakes import DeterministicProvider

REVIEWER = "tester <t@example.com>"


async def _fixture(
    session: AsyncSession, tmp_path: Path, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> Path:
    library = tmp_path / f"lib-{uuid7()}"
    library.mkdir()
    (library / "doc.md").write_text(
        "# 升级 fixture\n\n分块策略决定证据怎么切, 也决定引用能不能对齐。\n", encoding="utf-8"
    )
    gateway = ModelGateway(DeterministicProvider(), embedding_dimensions=1024)
    ingested = await ingest_markdown_file(
        session, gateway, path=Path("doc.md"), library_root=library
    )
    full = (
        await session.execute(
            text("SELECT full_text FROM document_versions WHERE id=:id"),
            {"id": ingested.version_id},
        )
    ).scalar_one()
    await session.rollback()
    quote = "分块策略决定证据怎么切"
    start = full.index(quote)
    rows = [
        {
            "item_key": "p1",
            "category": "single_hop",
            "language": "zh",
            "split": "dev",
            "question": "分块策略决定了什么?",
            "gold_answer": "决定证据怎么切, 也决定引用能不能对齐。",
            "gold_spans": [
                {
                    "version_id": str(ingested.version_id),
                    "char_start": start,
                    "char_end": start + len(quote),
                    "quote": quote,
                    "note": "fixture",
                }
            ],
            "gold_tools": [],
            "constraints": {
                "must_include": ["证据"],
                "must_not_include": [],
                "candidate_review": {"status": "pending_human", "item_key": "p1"},
            },
            "difficulty": 2,
            "temporal_ctx": None,
            "source_docs": ["D1"],
        }
    ]
    path = tmp_path / "items.json"
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def no_close() -> None:
        return None

    for module in (importer, promoter):
        monkeypatch.setattr(module, "session_factory", factory)
        monkeypatch.setattr(module, "close_database", no_close)
    await importer.run(path, apply=True)
    return path


def test_promotion_requires_an_explicit_reviewer(tmp_path: Path) -> None:
    with pytest.raises(CandidateSuiteError, match="必须显式给出复核人"):
        import asyncio

        asyncio.run(promote(tmp_path / "none.json", reviewer="  ", note="", apply=False))


@pytest.mark.integration
async def test_promotion_records_reviewer_and_is_idempotent(
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = await _fixture(db_session, tmp_path, db_engine, monkeypatch)

    dry = await promote(path, reviewer=REVIEWER, note="n", apply=False)
    assert dry["applied"] is False and dry["to_promote"] == 1
    still = (
        await db_session.execute(
            text("SELECT origin FROM eval_items WHERE question='分块策略决定了什么?'")
        )
    ).scalar_one()
    await db_session.rollback()
    assert still == "synthetic", "dry-run 不得改库"

    first = await promote(path, reviewer=REVIEWER, note="逐条复核通过", apply=True)
    second = await promote(path, reviewer=REVIEWER, note="逐条复核通过", apply=True)

    assert first["applied"] is True and first["to_promote"] == 1
    assert second["already_human"] == 1 and second["to_promote"] == 0

    row = (
        (
            await db_session.execute(
                text(
                    """
                    SELECT origin, constraints->'candidate_review' AS review,
                           validate_eval_spans(gold_spans) AS spans_valid
                    FROM eval_items WHERE question='分块策略决定了什么?'
                    """
                )
            )
        )
        .mappings()
        .one()
    )
    await db_session.rollback()
    assert row["origin"] == "human"
    assert row["spans_valid"]
    # 光翻 origin 不算数: 谁复核的、什么时候, 必须留在库里
    assert row["review"]["status"] == "approved"
    assert row["review"]["reviewer"] == REVIEWER
    assert row["review"]["reviewed_at"]
    assert row["review"]["promoted_from"] == "synthetic"


@pytest.mark.integration
async def test_promotion_refuses_when_stored_content_drifted(
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = await _fixture(db_session, tmp_path, db_engine, monkeypatch)
    async with db_session.begin():
        await db_session.execute(
            text(
                "UPDATE eval_items SET question='被改过的问题' WHERE question='分块策略决定了什么?'"
            )
        )

    with pytest.raises(CandidateSuiteError, match="复核对象与升级对象不是同一批"):
        await promote(path, reviewer=REVIEWER, note="n", apply=True)


@pytest.mark.integration
async def test_promotion_refuses_when_gold_span_no_longer_anchors(
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = await _fixture(db_session, tmp_path, db_engine, monkeypatch)
    # 模拟重新解析导致区间失效
    async with db_session.begin():
        await db_session.execute(
            text(
                """
                UPDATE eval_items
                SET gold_spans = jsonb_set(gold_spans, '{0,char_end}', '999999')
                WHERE question='分块策略决定了什么?'
                """
            )
        )

    with pytest.raises(CandidateSuiteError, match="锚不住原文"):
        await promote(path, reviewer=REVIEWER, note="n", apply=True)
