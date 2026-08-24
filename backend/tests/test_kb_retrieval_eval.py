from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pytest
from llama_index.core.embeddings import BaseEmbedding

from app.core.config import Settings
from app.rag.kb.index import KbHit, KbIndexError
from app.rag.kb.service import LocalKbService
from eval.kb_retrieval_runner import (
    IndexedNode,
    adaptive_rrf_min_score,
    load_catalog,
    run_evaluation,
)
from eval.kb_retrieval_suite import (
    StableGoldSpan,
    load_kb_retrieval_suite,
    select_suite_items,
)
from eval.report_metrics import KIND_RETRIEVAL, load_report


class FakeEmbedding(BaseEmbedding):
    @classmethod
    def class_name(cls) -> str:
        return "kb-eval-fake"

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * 1024
        for word in text.casefold().split():
            vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % len(vector)] += 1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]

    def _get_query_embedding(self, query: str) -> list[float]:
        return self._vector(query)

    def _get_text_embedding(self, text: str) -> list[float]:
        return self._vector(text)

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return self._vector(query)

    async def _aget_text_embedding(self, text: str) -> list[float]:
        return self._vector(text)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        knowledge_base_path=tmp_path / "kb",
        embedding_base_url="http://127.0.0.1:11434/v1",
        embedding_model="fake-embed",
        embedding_revision="test-v1",
        rerank_enabled=False,
    )


@pytest.fixture(autouse=True)
def stub_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.rag.kb import index as index_module

    monkeypatch.setattr(index_module, "build_embedding", lambda _settings: FakeEmbedding())


def _write_suite(path: Path, *, content_hash: str, anchor: dict[str, Any]) -> None:
    payload = {
        "schema_version": 1,
        "name": "kb-smoke-v1",
        "description": "runner engineering smoke",
        "origin": "synthetic",
        "review": {"status": "pending_human_review"},
        "items": [
            {
                "item_id": "answerable-1",
                "split": "dev",
                "category": "single_hop",
                "question": "What is the Atlas retention period?",
                "answerable": True,
                "gold_evidence_groups": [
                    {
                        "fact_id": "R1",
                        "alternatives": [
                            {
                                "content_hash": content_hash,
                                "page_no": anchor["page_no"],
                                "char_start": anchor["char_start"],
                                "char_end": anchor["char_end"],
                                "quote": anchor["quote"],
                            }
                        ],
                    }
                ],
            },
            {
                "item_id": "unanswerable-1",
                "split": "dev",
                "category": "unanswerable",
                "question": "What is the lunar banana protocol?",
                "answerable": False,
                "gold_evidence_groups": [],
            },
            {
                "item_id": "frozen-1",
                "split": "test",
                "category": "single_hop",
                "question": "Frozen duplicate",
                "answerable": True,
                "gold_evidence_groups": [
                    {
                        "fact_id": "R1",
                        "alternatives": [
                            {
                                "content_hash": content_hash,
                                "page_no": anchor["page_no"],
                                "char_start": anchor["char_start"],
                                "char_end": anchor["char_end"],
                                "quote": anchor["quote"],
                            }
                        ],
                    }
                ],
            },
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


async def _indexed_fixture(
    tmp_path: Path,
    settings: Settings,
) -> tuple[LocalKbService, dict[str, Any]]:
    source = tmp_path / "atlas.md"
    source.write_text(
        "# Atlas policy\n\nThe Atlas retention period is exactly 37 days.\n",
        encoding="utf-8",
    )
    noise = tmp_path / "noise.md"
    noise.write_text(
        "# Unrelated notes\n\nBananas orbit no production retention policy.\n",
        encoding="utf-8",
    )
    service = LocalKbService(settings.knowledge_base_path, settings=settings)
    service.create("Atlas", slug="atlas")
    await service.add_documents("atlas", [source, noise])
    catalog = load_catalog(
        service,
        settings=settings,
        kb_slug="atlas",
        kb_version_id="v1",
    )
    matches = catalog.find_quote("The Atlas retention period is exactly 37 days.")
    assert len(matches) == 1
    return service, matches[0]


@pytest.mark.asyncio
async def test_runner_uses_stable_content_anchors_and_writes_compatible_report(
    tmp_path: Path,
    settings: Settings,
) -> None:
    service, anchor = await _indexed_fixture(tmp_path, settings)
    suite_path = tmp_path / "suite.json"
    _write_suite(suite_path, content_hash=str(anchor["content_hash"]), anchor=anchor)
    suite = load_kb_retrieval_suite(suite_path, allow_synthetic=True)
    selected = select_suite_items(suite, include_test=False, test_access_note=None)

    package, report = await run_evaluation(
        suite=suite,
        items=selected,
        service=service,
        settings=settings,
        kb_slug="atlas",
        kb_version_id="v1",
        label="current-hybrid",
        top_k=1,
        diagnostic_k=2,
        token_budget=4000,
        theta=0.5,
        alpha=0.5,
        refusal_threshold=None,
        refusal_threshold_source=None,
        output_dir=tmp_path / "report",
        include_test=False,
        test_access_note=None,
    )

    assert report["metrics"]["error_count"] == 0
    assert report["metrics"]["span_recall_at_k"] == 1.0
    assert report["metrics"]["refusal"]["configured"] is None
    assert report["suite"]["selected_items"] == 2
    assert report["kb"]["index_fingerprint"]
    assert report["reproducibility"]["implementation_fingerprint"]
    assert (package / "report.md").is_file()
    loaded = load_report(package)
    assert loaded.kind == KIND_RETRIEVAL
    retrieved = loaded.items[0]["retrieved"][0]
    assert retrieved["content_hash"] == anchor["content_hash"]
    assert retrieved["char_end"] > retrieved["char_start"]


@pytest.mark.asyncio
async def test_catalog_rejects_a_stale_quote_before_running_queries(
    tmp_path: Path,
    settings: Settings,
) -> None:
    service, anchor = await _indexed_fixture(tmp_path, settings)
    catalog = load_catalog(
        service,
        settings=settings,
        kb_slug="atlas",
        kb_version_id="v1",
    )
    stale = StableGoldSpan(
        content_hash=str(anchor["content_hash"]),
        page_no=None,
        char_start=int(anchor["char_start"]),
        char_end=int(anchor["char_end"]),
        quote="X" + str(anchor["quote"])[1:],
    )

    with pytest.raises(ValueError, match="gold span 已漂移"):
        catalog.validate_span(stale)


def test_page_coordinates_share_document_identity_without_overlapping() -> None:
    first = StableGoldSpan(
        content_hash="a" * 64,
        page_no=1,
        char_start=10,
        char_end=18,
        quote="evidence",
    )
    second = StableGoldSpan(
        content_hash="a" * 64,
        page_no=2,
        char_start=10,
        char_end=18,
        quote="evidence",
    )

    assert first.document_id == second.document_id
    assert first.source_id != second.source_id
    assert first.metric_char_end < second.metric_char_start


@pytest.mark.asyncio
async def test_runner_stops_immediately_when_retrieval_infrastructure_is_unavailable(
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, anchor = await _indexed_fixture(tmp_path, settings)
    suite_path = tmp_path / "suite.json"
    _write_suite(suite_path, content_hash=str(anchor["content_hash"]), anchor=anchor)
    suite = load_kb_retrieval_suite(suite_path, allow_synthetic=True)
    selected = select_suite_items(suite, include_test=False, test_access_note=None)

    async def unavailable(*_args: object, **_kwargs: object) -> object:
        raise KbIndexError("embedding unavailable")

    monkeypatch.setattr("eval.kb_retrieval_runner.search_index", unavailable)
    output = tmp_path / "unavailable-report"
    with pytest.raises(KbIndexError, match="embedding unavailable"):
        await run_evaluation(
            suite=suite,
            items=selected,
            service=service,
            settings=settings,
            kb_slug="atlas",
            kb_version_id="v1",
            label="unavailable",
            top_k=1,
            diagnostic_k=2,
            token_budget=4000,
            theta=0.5,
            alpha=0.5,
            refusal_threshold=None,
            refusal_threshold_source=None,
            output_dir=output,
            include_test=False,
            test_access_note=None,
        )
    assert not output.exists()


@pytest.mark.asyncio
async def test_formal_top_k_is_not_replaced_by_deeper_diagnostic_ranking(
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, anchor = await _indexed_fixture(tmp_path, settings)
    suite_path = tmp_path / "suite.json"
    _write_suite(suite_path, content_hash=str(anchor["content_hash"]), anchor=anchor)
    suite = load_kb_retrieval_suite(suite_path, allow_synthetic=True)
    selected = select_suite_items(suite, include_test=False, test_access_note=None)
    catalog = load_catalog(
        service,
        settings=settings,
        kb_slug="atlas",
        kb_version_id="v1",
    )
    gold = catalog.by_node_id[
        next(
            node.node_id
            for node in catalog.nodes
            if node.content_hash == str(anchor["content_hash"])
        )
    ]
    noise = next(node for node in catalog.nodes if node.node_id != gold.node_id)

    def hit(node: IndexedNode, score: float) -> KbHit:
        return KbHit(
            node_id=node.node_id,
            text=node.text,
            score=score,
            doc_id="doc",
            filename=node.filename,
            title="",
            page_no=node.page_no,
            score_source="fusion",
        )

    calls: list[int] = []

    async def ranked(*_args: object, **kwargs: object) -> list[KbHit]:
        requested = int(kwargs["top_k"])
        calls.append(requested)
        return [hit(noise, 0.9)] if requested == 1 else [hit(noise, 0.9), hit(gold, 0.8)]

    monkeypatch.setattr("eval.kb_retrieval_runner.search_index", ranked)
    _package, report = await run_evaluation(
        suite=suite,
        items=selected,
        service=service,
        settings=settings,
        kb_slug="atlas",
        kb_version_id="v1",
        label="formal-vs-diagnostic",
        top_k=1,
        diagnostic_k=2,
        token_budget=4000,
        theta=0.5,
        alpha=0.5,
        refusal_threshold=None,
        refusal_threshold_source=None,
        output_dir=tmp_path / "formal-vs-diagnostic",
        include_test=False,
        test_access_note=None,
    )

    answerable = report["items"][0]
    assert answerable["retrieval"]["span_recall_at_k"] == 0.0
    assert len(answerable["retrieved"]) == 1
    assert len(answerable["diagnostic_retrieved"]) == 2
    assert answerable["span_diagnostics"][0]["status"] == "outside_top_k"
    assert calls == [1, 2, 1]


def test_adaptive_rrf_threshold_represents_rank_one_and_rank_four_consensus() -> None:
    assert adaptive_rrf_min_score(rrf_k=60, consensus_rank=4) == pytest.approx(1 / 60 + 1 / 63)


@pytest.mark.asyncio
async def test_adaptive_top_k_expands_low_consensus_queries_without_changing_fixed_top_k(
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, anchor = await _indexed_fixture(tmp_path, settings)
    suite_path = tmp_path / "suite.json"
    _write_suite(suite_path, content_hash=str(anchor["content_hash"]), anchor=anchor)
    suite = load_kb_retrieval_suite(suite_path, allow_synthetic=True)
    selected = select_suite_items(suite, include_test=False, test_access_note=None)
    catalog = load_catalog(
        service,
        settings=settings,
        kb_slug="atlas",
        kb_version_id="v1",
    )
    gold = catalog.by_node_id[
        next(
            node.node_id
            for node in catalog.nodes
            if node.content_hash == str(anchor["content_hash"])
        )
    ]
    noise = next(node for node in catalog.nodes if node.node_id != gold.node_id)

    def hit(node: IndexedNode, score: float) -> KbHit:
        return KbHit(
            node_id=node.node_id,
            text=node.text,
            score=score,
            doc_id="doc",
            filename=node.filename,
            title="",
            page_no=node.page_no,
            score_source="fusion",
        )

    calls: list[int] = []

    async def ranked(*_args: object, **kwargs: object) -> list[KbHit]:
        requested = int(kwargs["top_k"])
        calls.append(requested)
        return [hit(noise, 0.01)] if requested == 1 else [hit(noise, 0.04), hit(gold, 0.03)]

    monkeypatch.setattr("eval.kb_retrieval_runner.search_index", ranked)
    _package, report = await run_evaluation(
        suite=suite,
        items=selected,
        service=service,
        settings=settings,
        kb_slug="atlas",
        kb_version_id="v1",
        label="adaptive-top-k",
        top_k=1,
        diagnostic_k=2,
        token_budget=4000,
        theta=0.5,
        alpha=0.5,
        refusal_threshold=None,
        refusal_threshold_source=None,
        output_dir=tmp_path / "adaptive-top-k",
        include_test=False,
        test_access_note=None,
        adaptive_top_k_enabled=True,
        adaptive_max_top_k=2,
        adaptive_consensus_rank=4,
    )

    answerable = report["items"][0]
    assert answerable["adaptive_expanded"] is True
    assert answerable["effective_top_k"] == 2
    assert answerable["initial_top_score"] == pytest.approx(0.01)
    assert answerable["retrieval"]["span_recall_at_k"] == 1.0
    assert report["config"]["adaptive_top_k"]["enabled"] is True
    assert report["metrics"]["adaptive_expanded_count"] == 2
    assert calls == [1, 2, 1, 2]


def test_suite_provenance_and_test_split_are_fail_closed(tmp_path: Path) -> None:
    quote = "evidence"
    anchor: dict[str, Any] = {
        "content_hash": "a" * 64,
        "page_no": None,
        "char_start": 0,
        "char_end": len(quote),
        "quote": quote,
    }
    path = tmp_path / "suite.json"
    _write_suite(path, content_hash="a" * 64, anchor=anchor)

    with pytest.raises(ValueError, match="allow-synthetic"):
        load_kb_retrieval_suite(path)
    suite = load_kb_retrieval_suite(path, allow_synthetic=True)
    assert [
        item.item_id
        for item in select_suite_items(suite, include_test=False, test_access_note=None)
    ] == ["answerable-1", "unanswerable-1"]
    with pytest.raises(ValueError, match="test-access-note"):
        select_suite_items(suite, include_test=True, test_access_note=None)
    assert len(select_suite_items(suite, include_test=True, test_access_note="release gate")) == 3


def test_checked_in_rag_research_candidate_suite_has_the_declared_shape() -> None:
    suite_path = (
        Path(__file__).resolve().parents[2] / "eval" / "suites" / "kb-rag-research-dev-v1.json"
    )

    suite = load_kb_retrieval_suite(suite_path, allow_synthetic=True)

    assert suite.origin == "synthetic"
    assert suite.review_status == "pending_human_review"
    assert len(suite.items) == 26
    assert sum(item.answerable for item in suite.items) == 22
    assert {item.category for item in suite.items} == {
        "exact_identifier",
        "semantic_single_hop",
        "multi_hop",
        "unanswerable",
    }


def test_refusal_calibration_suite_is_independent_from_evaluation_gold() -> None:
    suites = Path(__file__).resolve().parents[2] / "eval" / "suites"
    evaluation = load_kb_retrieval_suite(
        suites / "kb-rag-research-dev-v1.json",
        allow_synthetic=True,
    )
    calibration = load_kb_retrieval_suite(
        suites / "kb-rag-research-refusal-calibration-v1.json",
        allow_synthetic=True,
    )

    evaluation_documents = {span.content_hash for item in evaluation.items for span in item.spans}
    calibration_documents = {span.content_hash for item in calibration.items for span in item.spans}

    assert calibration.review_status == "pending_human_review"
    assert len(calibration.items) == 12
    assert sum(item.answerable for item in calibration.items) == 8
    assert evaluation_documents.isdisjoint(calibration_documents)


def test_kb_suite_human_origin_requires_complete_auditable_signoff(tmp_path: Path) -> None:
    quote = "evidence"
    anchor: dict[str, Any] = {
        "content_hash": "a" * 64,
        "page_no": None,
        "char_start": 0,
        "char_end": len(quote),
        "quote": quote,
    }
    path = tmp_path / "suite.json"
    _write_suite(path, content_hash="a" * 64, anchor=anchor)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["origin"] = "human"

    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="human 评测集必须是 approved"):
        load_kb_retrieval_suite(path)

    payload["review"] = {
        "status": "approved",
        "reviewer": "fixture-owner",
        "reviewed_at": "2026-08-24T09:30:00+08:00",
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    suite = load_kb_retrieval_suite(path)
    assert suite.reviewer == "fixture-owner"

    payload["review"]["reviewed_at"] = "2026-08-24 09:30:00"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="包含时区"):
        load_kb_retrieval_suite(path)
