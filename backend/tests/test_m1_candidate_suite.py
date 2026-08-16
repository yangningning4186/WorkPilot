import re
from collections import Counter
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from eval.build_m1_candidate_suite import (
    EXPECTED_CANDIDATE_COUNTS,
    GROUPS,
    TEST_CATEGORY_TARGET,
    BlockAnchor,
    CandidateItem,
    CandidateSuiteError,
    audit_content_quality,
    build_group_items,
    fingerprint_items,
    import_candidates,
    stable_test_groups,
    validate_candidate_items,
    write_outputs,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _blocks(version_id: str) -> list[BlockAnchor]:
    blocks: list[BlockAnchor] = []
    cursor = 0
    for index in range(30):
        quote = (
            f"Evidence 😀 段落 {index}: deterministic Unicode text for validation, "
            "with enough content to pass the real evidence-length filter."
        )
        blocks.append(
            BlockAnchor(
                version_id=version_id,
                block_idx=index,
                block_type="table" if index >= 20 else "paragraph",
                char_start=cursor,
                char_end=cursor + len(quote),
                quote=quote,
                heading_path=(f"Section {index}",),
            )
        )
        cursor += len(quote) + 1
    return blocks


def _candidate_items() -> list[CandidateItem]:
    test_groups = stable_test_groups()
    items: list[CandidateItem] = []
    for index, group in enumerate(GROUPS):
        version_id = f"00000000-0000-0000-0000-{index + 1:012d}"
        split = "test" if group.key in test_groups else "dev"
        items.extend(
            build_group_items(
                group,
                split=split,
                version_id=version_id,
                activated_at="2026-08-16T00:00:00+00:00",
                blocks=_blocks(version_id),
            )
        )
    return items


def test_stable_split_is_document_isolated_and_exactly_stratified() -> None:
    items = _candidate_items()
    summary = validate_candidate_items(items, human_versions={"human-only-version"})

    assert summary["split_counts"] == {"dev": 60, "test": 20}
    assert summary["language_counts"] == {
        "dev:en": 30,
        "dev:zh": 30,
        "test:en": 10,
        "test:zh": 10,
    }
    assert Counter(item.category for item in items) == EXPECTED_CANDIDATE_COUNTS
    assert Counter(item.category for item in items if item.split == "test") == TEST_CATEGORY_TARGET
    assert {item.partition_version_id for item in items if item.split == "dev"}.isdisjoint(
        {item.partition_version_id for item in items if item.split == "test"}
    )


def test_candidate_fingerprint_is_order_independent_and_repeatable() -> None:
    items = _candidate_items()

    assert fingerprint_items(items) == fingerprint_items(list(reversed(items)))
    assert fingerprint_items(items) == fingerprint_items(_candidate_items())


@pytest.mark.parametrize("leak", ["version", "question", "span"])
def test_candidate_validation_rejects_cross_split_leakage(leak: str) -> None:
    items = _candidate_items()
    dev_index = next(
        index for index, item in enumerate(items) if item.split == "dev" and item.gold_spans
    )
    test_item = next(item for item in items if item.split == "test" and item.gold_spans)
    dev_item = items[dev_index]
    if leak == "version":
        items[dev_index] = replace(dev_item, partition_version_id=test_item.partition_version_id)
    elif leak == "question":
        items[dev_index] = replace(dev_item, question=test_item.question)
    else:
        items[dev_index] = replace(dev_item, gold_spans=test_item.gold_spans)

    with pytest.raises(CandidateSuiteError, match=r"cross-split|duplicate questions"):
        validate_candidate_items(items, human_versions=set())


def test_candidate_validation_rejects_human_or_premature_review_status() -> None:
    items = _candidate_items()
    items[0] = replace(items[0], origin="human")
    with pytest.raises(CandidateSuiteError, match="origin must be synthetic"):
        validate_candidate_items(items, human_versions=set())


def test_candidate_validation_rejects_question_copied_from_existing_human() -> None:
    items = _candidate_items()
    normalized = " ".join(re.findall(r"\w+", items[0].question.casefold(), flags=re.UNICODE))

    with pytest.raises(CandidateSuiteError, match="existing-vs-candidate"):
        validate_candidate_items(
            items,
            human_versions=set(),
            human_questions={normalized},
        )


def test_automatic_drafts_are_rejected_by_content_quality_gate() -> None:
    quality = audit_content_quality(_candidate_items())

    assert quality["status"] == "rejected_content_quality"
    assert quality["finding_counts"]["generic_question_template"] > 0
    assert quality["finding_counts"]["raw_quote_as_gold_answer"] > 0

    items = _candidate_items()
    constraints = {**items[0].constraints, "candidate_review": {"status": "approved"}}
    items[0] = replace(items[0], constraints=constraints)
    with pytest.raises(CandidateSuiteError, match="pending_human"):
        validate_candidate_items(items, human_versions=set())


def test_report_writes_separate_dev_and_test_review_files(tmp_path: Path) -> None:
    items = _candidate_items()
    validation = validate_candidate_items(items, human_versions=set())
    manifest = write_outputs(
        tmp_path,
        items,
        {
            "human": {"item_count": 40},
            "validation": validation,
            "test_groups": sorted(stable_test_groups()),
            "full_category_counts": {},
        },
        None,
    )

    assert manifest.exists()
    assert len((manifest.parent / "review-dev.jsonl").read_text().splitlines()) == 60
    assert len((manifest.parent / "review-test.jsonl").read_text().splitlines()) == 20
    assert "不得用于阈值" in (manifest.parent / "report.md").read_text()


@pytest.mark.integration
async def test_candidate_import_is_idempotent_and_never_promotes_origin(
    db_session: AsyncSession,
) -> None:
    sample = [
        item for item in _candidate_items() if item.category in {"unanswerable", "agent_task"}
    ]

    first = await import_candidates(db_session, sample)
    second = await import_candidates(db_session, sample)

    assert first == second
    assert sum(dataset["item_count"] for dataset in first.values()) == len(sample)
    assert {dataset["origin"] for dataset in first.values()} == {"synthetic"}
    assert {dataset["review_status"] for dataset in first.values()} == {"pending_human"}


@pytest.mark.integration
async def test_candidate_import_rejects_same_id_with_tampered_content(
    db_session: AsyncSession,
) -> None:
    sample = [
        item for item in _candidate_items() if item.category in {"unanswerable", "agent_task"}
    ]
    imported = await import_candidates(db_session, sample)
    dataset_id = next(
        UUID(dataset["dataset_id"]) for dataset in imported.values() if dataset["item_count"]
    )
    async with db_session.begin():
        await db_session.execute(
            text(
                """
                UPDATE eval_items SET question='tampered'
                WHERE id=(SELECT id FROM eval_items WHERE dataset_id=:dataset_id LIMIT 1)
                """
            ),
            {"dataset_id": dataset_id},
        )

    with pytest.raises(CandidateSuiteError, match="内容与 manifest 漂移"):
        await import_candidates(db_session, sample)
