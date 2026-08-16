"""人工撰写候选题的导入纪律: 与自动草稿共用同一套 fail-closed 校验。"""

import json
from pathlib import Path

import eval.build_m1_candidate_suite as builder
import eval.import_handwritten_suite as importer
import pytest
from eval.build_m1_candidate_suite import CandidateSuiteError
from eval.import_handwritten_suite import (
    TARGET_DATASETS,
    load_items,
    preflight,
    run,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from uuid6 import uuid7

from app.llm.gateway import ModelGateway
from app.services.markdown_ingestion import ingest_markdown_file
from tests.fakes import DeterministicProvider

V1 = "00000000-0000-0000-0000-000000000001"
V2 = "00000000-0000-0000-0000-000000000002"


def _span(version: str, start: int, end: int, quote: str) -> dict:
    return {
        "version_id": version,
        "char_start": start,
        "char_end": end,
        "quote": quote,
        "note": "fixture",
    }


def _raw(key: str, **over) -> dict:
    base = {
        "item_key": key,
        "category": "single_hop",
        "language": "zh",
        "split": "dev",
        "question": f"问题 {key}?",
        "gold_answer": f"综述答案 {key}, 与证据不同字。",
        "gold_spans": [_span(V1, 10, 40, "这是一段足够长的证据原文用于校验。")],
        "gold_tools": [],
        "constraints": {
            "must_include": ["综述"],
            "must_not_include": [],
            "candidate_review": {"status": "pending_human", "item_key": key},
        },
        "difficulty": 2,
        "temporal_ctx": None,
        "source_docs": ["D1"],
    }
    base.update(over)
    return base


def _write(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "items.json"
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return path


def test_preflight_accepts_a_clean_batch(tmp_path: Path) -> None:
    items = load_items(_write(tmp_path, [_raw("a"), _raw("b", language="en")]))

    summary = preflight(items)

    assert summary["item_count"] == 2
    assert summary["content_quality"] == "passed"
    assert summary["dev_test_version_overlap"] == 0
    # item_key 带套件前缀, 避免与自动草稿套件派生出同一个 uuid5
    assert all(item.item_key.startswith("m1-handwritten-40-v1:") for item in items)


def test_preflight_rejects_dev_test_version_leakage(tmp_path: Path) -> None:
    rows = [_raw("a"), _raw("b", split="test")]  # 两条都用 V1
    items = load_items(_write(tmp_path, rows))

    with pytest.raises(CandidateSuiteError, match="dev/test 共用 document version"):
        preflight(items)


def test_preflight_rejects_raw_quote_answers_and_duplicates(tmp_path: Path) -> None:
    quote = "这是一段足够长的证据原文用于校验。"
    raw_answer = _raw(
        "a",
        gold_answer=quote,
        constraints={
            "must_include": ["证据"],
            "must_not_include": [],
            "candidate_review": {"status": "pending_human", "item_key": "a"},
        },
    )
    with pytest.raises(CandidateSuiteError, match="内容质量门禁未通过"):
        preflight(load_items(_write(tmp_path, [raw_answer])))

    dup = [_raw("a"), _raw("b", question=_raw("a")["question"])]
    with pytest.raises(CandidateSuiteError, match="问题重复"):
        preflight(load_items(_write(tmp_path, dup)))


def test_preflight_rejects_items_already_marked_reviewed(tmp_path: Path) -> None:
    approved = _raw(
        "a",
        constraints={
            "must_include": ["综述"],
            "must_not_include": [],
            "candidate_review": {"status": "approved", "item_key": "a"},
        },
    )
    with pytest.raises(CandidateSuiteError, match="pending_human"):
        preflight(load_items(_write(tmp_path, [approved])))


async def _real_version(session: AsyncSession, tmp_path: Path) -> tuple[str, str, int, int]:
    """灌一篇真实文档, 返回 version_id 与一段真实存在的字符区间。"""
    library = tmp_path / f"lib-{uuid7()}"
    library.mkdir()
    (library / "doc.md").write_text(
        "# 手写候选集 fixture\n\n分块策略决定证据怎么切, 也决定引用能不能对齐。\n",
        encoding="utf-8",
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
    return str(ingested.version_id), quote, start, start + len(quote)


@pytest.mark.integration
async def test_import_is_idempotent_and_keeps_spans_valid(
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version_id, quote, start, end = await _real_version(db_session, tmp_path)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def no_close() -> None:
        return None

    monkeypatch.setattr(importer, "session_factory", factory)
    monkeypatch.setattr(importer, "close_database", no_close)

    span = [_span(version_id, start, end, quote)]
    rows = [
        _raw("a", gold_spans=span),
        _raw("b", language="en", gold_spans=span),
        # test 侧用 unanswerable: 没有 span, 天然不与 dev 共用 version
        _raw(
            "c",
            split="test",
            category="unanswerable",
            gold_answer=None,
            gold_spans=[],
            constraints={
                "must_include": [],
                "must_not_include": [],
                "candidate_review": {"status": "pending_human", "item_key": "c"},
            },
        ),
    ]
    path = _write(tmp_path, rows)

    first = await run(path, apply=True)
    second = await run(path, apply=True)

    assert first["import"]["applied"] is True
    assert second["import"]["datasets"] == first["import"]["datasets"]

    stored = (
        (
            await db_session.execute(
                text(
                    """
                    SELECT i.origin,
                           i.constraints->'candidate_review'->>'status' AS status,
                           validate_eval_spans(i.gold_spans) AS spans_valid
                    FROM eval_items i JOIN eval_datasets d ON d.id=i.dataset_id
                    WHERE d.name = ANY(:names)
                    """
                ),
                {"names": sorted(TARGET_DATASETS.values())},
            )
        )
        .mappings()
        .all()
    )
    assert len(stored) == 3
    assert {row["origin"] for row in stored} == {"synthetic"}
    assert {row["status"] for row in stored} == {"pending_human"}
    # 落库的 gold span 必须仍然锚得住原文, 否则标注等于废了
    assert all(row["spans_valid"] for row in stored)


@pytest.mark.integration
async def test_import_rejects_content_drift_on_the_same_key(
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version_id, quote, start, end = await _real_version(db_session, tmp_path)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def no_close() -> None:
        return None

    monkeypatch.setattr(importer, "session_factory", factory)
    monkeypatch.setattr(importer, "close_database", no_close)

    span = [_span(version_id, start, end, quote)]
    await run(_write(tmp_path, [_raw("a", gold_spans=span)]), apply=True)

    drifted = tmp_path / "drift.json"
    drifted.write_text(
        json.dumps(
            [_raw("a", gold_spans=span, question="换了一个完全不同的问题?")],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(builder.CandidateSuiteError, match="漂移"):
        await run(drifted, apply=True)
