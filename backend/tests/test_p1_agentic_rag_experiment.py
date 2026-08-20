from uuid import UUID

import pytest

from app.rag.retrieval.dense import DenseSearchHit
from eval.agentic_retrieval import (
    AgenticPlanError,
    RetrievalRequirement,
    build_missing_requirement,
    evidence_ledger_top_k,
    parse_agentic_plan,
    rank_documents_with_hints,
    section_navigation_candidates,
    select_documents_for_requirements,
)
from eval.p1_agentic_rag_experiment import (
    VARIANTS,
    _with_transport_retries,
    summarize_items,
)


def _hit(
    number: int,
    *,
    version: int | None = None,
    title: str | None = None,
    heading: tuple[str, ...] = (),
    char_start: int | None = None,
) -> DenseSearchHit:
    version = version or number
    return DenseSearchHit(
        chunk_id=UUID(f"00000000-0000-0000-0000-{number:012d}"),
        document_id=UUID(f"10000000-0000-0000-0000-{version:012d}"),
        version_id=UUID(f"20000000-0000-0000-0000-{version:012d}"),
        version_no=1,
        title=title or f"doc-{version}",
        source_uri=f"test://doc-{version}",
        content=f"evidence-{number}",
        score=1.0,
        dense_score=1.0,
        heading_path=list(heading),
        blocks=[],
        char_start=char_start if char_start is not None else number * 100,
        char_end=(char_start if char_start is not None else number * 100) + 50,
    )


def test_parse_agentic_plan_preserves_requirements_and_hints() -> None:
    value = """prefix {
      "decomposed": true,
      "reason": "需要比较两个系统",
      "requirements": [
        {"id":"R1","query":"AutoGen 使用什么执行框架？",
         "entities":["AutoGen"],"document_hints":["AutoGen paper"]},
        {"id":"R2","query":"CODESKILL 使用什么执行框架？",
         "entities":["CODESKILL"],"document_hints":["CODESKILL"]}
      ]
    } suffix"""

    decomposed, reason, requirements = parse_agentic_plan(
        value,
        original_query="比较 AutoGen 和 CODESKILL 的执行框架",
    )

    assert decomposed is True
    assert reason == "需要比较两个系统"
    assert [item.id for item in requirements] == ["R1", "R2"]
    assert requirements[0].entities == ("AutoGen",)
    assert requirements[1].document_hints == ("CODESKILL",)


def test_parse_agentic_plan_rejects_fake_decomposition() -> None:
    with pytest.raises(AgenticPlanError, match="至少需要 2 个"):
        parse_agentic_plan(
            '{"decomposed":true,"reason":"x","requirements":['
            '{"id":"R1","query":"only one","entities":[],"document_hints":[]}] }',
            original_query="original",
        )


def test_document_discovery_reserves_one_slot_per_requirement() -> None:
    shared = _hit(1, version=1)
    left = _hit(2, version=2)
    right = _hit(3, version=3)
    fallback = _hit(4, version=4)

    selected = select_documents_for_requirements(
        [[shared, left], [shared, right]],
        [shared, fallback],
        max_documents=3,
    )

    assert [hit.version_id for hit in selected] == [
        shared.version_id,
        left.version_id,
        right.version_id,
    ]


def test_document_hint_only_stably_boosts_matching_title() -> None:
    first = _hit(1, title="Generic agent survey")
    target = _hit(2, title="The AutoGen System")
    requirement = RetrievalRequirement(
        id="R1",
        query="AutoGen 的执行框架是什么",
        entities=("AutoGen",),
        document_hints=("AutoGen",),
    )

    ranked = rank_documents_with_hints([first, target], requirement)

    assert ranked == [target, first]


def test_section_navigation_expands_neighbors_and_same_section() -> None:
    before = _hit(1, heading=("intro",), char_start=0)
    seed = _hit(2, heading=("method",), char_start=100)
    after = _hit(3, heading=("method",), char_start=200)
    same_section = _hit(4, heading=("method",), char_start=300)
    other_seed = _hit(5, heading=("results",), char_start=400)
    ranking = [seed, other_seed, same_section, before, after]

    selected = section_navigation_candidates(
        ranking,
        top_n=5,
        section_seed_k=1,
        neighbor_radius=1,
    )

    assert [hit.chunk_id for hit in selected] == [
        seed.chunk_id,
        before.chunk_id,
        after.chunk_id,
        same_section.chunk_id,
        other_seed.chunk_id,
    ]


def test_evidence_ledger_keeps_distinct_support_per_requirement() -> None:
    shared = _hit(1)
    left = _hit(2)
    right = _hit(3)
    filler = _hit(4)

    selected = evidence_ledger_top_k(
        [[shared, left], [shared, right]],
        [filler, left, right],
        top_k=3,
    )

    assert [hit.chunk_id for hit in selected] == [
        shared.chunk_id,
        right.chunk_id,
        filler.chunk_id,
    ]


def test_missing_requirement_is_explicitly_scoped() -> None:
    requirement = build_missing_requirement(
        "比较两个系统",
        ["缺少系统 A 的基座模型", "缺少系统 B 的数据集"],
        entities=("A", "B"),
    )

    assert requirement.id == "R_missing"
    assert "仅检索以下缺失事实" in requirement.query
    assert "基座模型" in requirement.query


def test_summary_uses_target_regression_and_safety_axes() -> None:
    items: list[dict[str, object]] = []
    for index in range(70):
        answerable = index < 57
        target = index < 14
        variants: dict[str, object] = {}
        for name in VARIANTS:
            baseline = name == "rrf_top5"
            variants[name] = {
                "complete_evidence": answerable and (index >= 14 or not baseline),
                "gate_sufficient": answerable,
                "gate_invalid": False,
                "refinement_applied": name == "agentic_navigation" and target,
                "logical_model_calls": 2 if baseline else 4,
                "reranker_calls": 0 if baseline else 1,
                "latency_ms": 100.0 if baseline else 200.0,
            }
        items.append(
            {
                "answerable": answerable,
                "target_axis": target,
                "variants": variants,
            }
        )

    summary = summarize_items(items)
    by_variant = summary["by_variant"]

    assert isinstance(by_variant, dict)
    assert by_variant["rrf_top5"]["target_complete"] == 0
    assert by_variant["agentic_navigation"]["target_complete"] == 14
    assert by_variant["agentic_navigation"]["target_rescued_vs_baseline"] == 14
    assert by_variant["agentic_navigation"]["unanswerable_refused"] == 13
    assert by_variant["agentic_navigation"]["refinement_applied"] == 14
    comparisons = summary["vs_baseline"]
    assert isinstance(comparisons, dict)
    agentic = comparisons["agentic_navigation"]
    assert agentic["target_complete"]["delta"] == 1.0
    assert agentic["unanswerable_refused"]["delta"] == 0.0


@pytest.mark.asyncio
async def test_transport_retry_is_bounded_and_reported() -> None:
    calls = 0

    async def operation() -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls < 3:
            import httpx

            raise httpx.RemoteProtocolError("transient disconnect")
        return {"ok": True}

    row, retries = await _with_transport_retries(operation)

    assert row == {"ok": True}
    assert retries == 2
    assert calls == 3
