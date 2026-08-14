from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.api.dependencies import get_model_gateway
from app.core.db import get_db_session
from app.llm.audit import SqlLlmCallAudit
from app.llm.gateway import ModelGateway
from app.main import create_app
from app.retrieval.dense import DenseSearchHit, _merge_query_rankings
from app.retrieval.fusion import reciprocal_rank_fusion
from app.retrieval.lexical import lexical_search, lexical_terms
from app.services.evidence_sufficiency import (
    EvidenceAssessmentError,
    parse_evidence_assessment,
)
from app.services.grounded_answer import (
    RetrievalRefusalSignals,
    answer_with_citations,
    evaluate_refusal,
)
from app.services.markdown_ingestion import ingest_markdown_file
from app.services.query_decomposition import QueryDecompositionError, parse_query_plan
from app.services.reranker import RerankResponseError, parse_rerank_response
from tests.fakes import DeterministicProvider


def test_refusal_threshold_is_strict_and_handles_empty_results() -> None:
    hit = DenseSearchHit(
        chunk_id=uuid7(),
        document_id=uuid7(),
        version_id=uuid7(),
        version_no=1,
        title="test",
        source_uri="test.md",
        content="evidence",
        score=0.5,
        heading_path=[],
        blocks=[],
    )

    assert evaluate_refusal([], threshold=0.5) == RetrievalRefusalSignals(
        None, None, None, True, "no_evidence"
    )
    assert evaluate_refusal([hit], threshold=0.5) == RetrievalRefusalSignals(
        0.5, None, None, True, None
    )
    assert evaluate_refusal([hit], threshold=0.5001) == RetrievalRefusalSignals(
        0.5, None, None, True, "below_threshold"
    )

    with pytest.raises(ValueError, match="-1 到 1"):
        evaluate_refusal([hit], threshold=1.1)


def test_refusal_signals_include_top1_top2_margin() -> None:
    def hit(score: float) -> DenseSearchHit:
        return DenseSearchHit(
            chunk_id=uuid7(),
            document_id=uuid7(),
            version_id=uuid7(),
            version_no=1,
            title="test",
            source_uri="test.md",
            content="evidence",
            score=score,
            heading_path=[],
            blocks=[],
        )

    signals = evaluate_refusal([hit(0.7), hit(0.68)], threshold=0.5)

    assert signals.top_score == 0.7
    assert signals.second_score == 0.68
    assert signals.score_margin == pytest.approx(0.02)
    assert signals.low_margin is True


def test_evidence_assessment_parser_is_strict_and_accepts_wrapped_json() -> None:
    parsed = parse_evidence_assessment(
        '```json\n{"sufficient":true,"reason":"证据明确",'
        '"support_ids":["S1"],"missing_aspects":[]}\n```',
        allowed_ids={"S1"},
    )

    assert parsed["sufficient"] is True
    assert parsed["support_ids"] == ["S1"]

    with pytest.raises(EvidenceAssessmentError, match="未知标签"):
        parse_evidence_assessment(
            '{"sufficient":true,"reason":"证据明确","support_ids":["S99"],"missing_aspects":[]}',
            allowed_ids={"S1"},
        )


def test_query_plan_parser_requires_two_distinct_subqueries() -> None:
    decomposed, reason, queries = parse_query_plan(
        '{"decomposed":true,"reason":"需要两项事实","queries":["项目 A 指标",'
        '"项目 B 指标","项目 A 指标"]}',
        original_query="比较项目 A 和项目 B 的指标",
    )

    assert decomposed is True
    assert reason == "需要两项事实"
    assert queries == ["项目 A 指标", "项目 B 指标"]

    with pytest.raises(QueryDecompositionError, match="至少需要 2 条"):
        parse_query_plan(
            '{"decomposed":true,"reason":"错误分解","queries":["项目 A 指标"]}',
            original_query="比较项目 A 和项目 B 的指标",
        )


def test_multi_query_fusion_keeps_head_evidence_from_each_query() -> None:
    shared = DenseSearchHit(
        chunk_id=uuid7(),
        document_id=uuid7(),
        version_id=uuid7(),
        version_no=1,
        title="shared",
        source_uri="shared.md",
        content="shared",
        score=0.7,
        heading_path=[],
        blocks=[],
    )
    left = DenseSearchHit(
        chunk_id=uuid7(),
        document_id=uuid7(),
        version_id=uuid7(),
        version_no=1,
        title="left",
        source_uri="left.md",
        content="left",
        score=0.8,
        heading_path=[],
        blocks=[],
    )
    right = DenseSearchHit(
        chunk_id=uuid7(),
        document_id=uuid7(),
        version_id=uuid7(),
        version_no=1,
        title="right",
        source_uri="right.md",
        content="right",
        score=0.75,
        heading_path=[],
        blocks=[],
    )

    fused = _merge_query_rankings([[left, shared], [right, shared]], top_k=3)

    assert [item.chunk_id for item in fused] == [
        left.chunk_id,
        right.chunk_id,
        shared.chunk_id,
    ]


def test_rerank_parser_requires_complete_unique_ranking() -> None:
    ranking = parse_rerank_response(
        '{"ranking":[{"id":"C2","score":0.9},{"id":"C1","score":0.2}]}',
        allowed_ids={"C1", "C2"},
    )

    assert ranking == [("C2", 0.9), ("C1", 0.2)]
    with pytest.raises(RerankResponseError, match="未覆盖全部候选"):
        parse_rerank_response(
            '{"ranking":[{"id":"C1","score":0.5}]}',
            allowed_ids={"C1", "C2"},
        )


def test_lexical_terms_and_rrf_cover_exact_identifier() -> None:
    assert "zxq-900" in lexical_terms("比较 ZXQ-900 的吞吐量")
    dense_hit = DenseSearchHit(
        chunk_id=uuid7(),
        document_id=uuid7(),
        version_id=uuid7(),
        version_no=1,
        title="dense",
        source_uri="dense.md",
        content="dense",
        score=0.8,
        heading_path=[],
        blocks=[],
        dense_score=0.8,
    )
    lexical_hit = DenseSearchHit(
        chunk_id=uuid7(),
        document_id=uuid7(),
        version_id=uuid7(),
        version_no=1,
        title="lexical",
        source_uri="lexical.md",
        content="lexical",
        score=1.0,
        heading_path=[],
        blocks=[],
        lexical_score=1.0,
    )

    fused = reciprocal_rank_fusion([[dense_hit], [lexical_hit]], top_k=2)

    assert {item.chunk_id for item in fused} == {dense_hit.chunk_id, lexical_hit.chunk_id}
    assert all(item.fusion_score is not None for item in fused)


@pytest.mark.integration
async def test_lexical_search_matches_exact_identifier(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    (library / "metric.md").write_text(
        "# 压测\n\n型号 ZXQ-900 的吞吐量是 321 QPS。\n",
        encoding="utf-8",
    )
    provider = DeterministicProvider()
    gateway = ModelGateway(provider, embedding_dimensions=1024)
    await ingest_markdown_file(
        db_session,
        gateway,
        path=Path("metric.md"),
        library_root=library,
    )

    hits = await lexical_search(db_session, query="ZXQ-900 吞吐量", top_k=5)

    assert hits
    assert hits[0].source_uri == "metric.md"
    assert hits[0].lexical_score is not None


@pytest.mark.integration
async def test_retrieval_generation_and_block_citation_chain(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    (library / "dense.md").write_text(
        "# 检索\n\n## 稠密检索\n\n稠密检索通过向量相似度召回语义相关内容。\n",
        encoding="utf-8",
    )
    sufficient = (
        '{"sufficient":true,"reason":"S1 明确回答了问题","support_ids":["S1"],"missing_aspects":[]}'
    )
    provider = DeterministicProvider(
        completion_texts=[sufficient, "稠密检索使用向量相似度召回语义相关内容。[S1]"]
    )
    gateway = ModelGateway(
        provider,
        embedding_dimensions=1024,
        audit_sink=SqlLlmCallAudit(db_session),
    )
    await ingest_markdown_file(
        db_session,
        gateway,
        path=Path("dense.md"),
        library_root=library,
    )

    result = await answer_with_citations(
        db_session,
        gateway,
        query="稠密检索如何召回内容?",
        top_k=1,
        query_decomposition_enabled=False,
    )
    await db_session.commit()

    assert result.refused is False
    assert result.refusal_reason is None
    assert result.evidence_sufficient is True
    assert result.evidence_model == "fake-chat"
    assert result.top_score is not None
    assert result.top_score >= result.threshold
    assert result.answer.endswith("[S1]")
    assert len(result.citations) == 1
    citation = result.citations[0]
    assert citation.quote == "稠密检索通过向量相似度召回语义相关内容。"
    full_text = (
        await db_session.execute(
            text("SELECT full_text FROM document_versions WHERE id=:id"),
            {"id": citation.version_id},
        )
    ).scalar_one()
    assert full_text[citation.char_start : citation.char_end] == citation.quote
    assert provider.last_messages[0].role == "system"
    assert "只能依据本次提供的证据" in provider.last_messages[0].content

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def override_gateway() -> AsyncIterator[ModelGateway]:
        yield gateway

    app = create_app()
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_model_gateway] = override_gateway
    provider.queue_completions(
        sufficient,
        "稠密检索使用向量相似度召回语义相关内容。[S1]",
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/answer",
            json={"query": "稠密检索如何召回内容?", "top_k": 1},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["refused"] is False
        assert payload["refusal_reason"] is None
        assert payload["top_score"] >= payload["threshold"]
        assert payload["citations"][0]["block_id"] == str(citation.block_id)
        assert payload["citations"][0]["quote"] == citation.quote

        provider.queue_completions(
            sufficient,
            "这是一个带伪引用的回答。[S99]",
        )
        invalid = await client.post(
            "/api/v1/answer",
            json={"query": "稠密检索如何召回内容?", "top_k": 1},
        )
        assert invalid.status_code == 502
        assert invalid.json()["detail"]["code"] == "invalid_model_citation"
        assert invalid.json()["detail"]["unknown_ids"] == ["S99"]
    app.dependency_overrides.clear()

    task_types = (
        await db_session.execute(text("SELECT task_type FROM llm_calls ORDER BY created_at"))
    ).scalars()
    assert set(task_types) == {
        "document_embedding",
        "query_embedding",
        "evidence_sufficiency",
        "grounded_answer",
    }


@pytest.mark.integration
async def test_low_score_refuses_before_generation(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    (library / "dense.md").write_text(
        "# 检索\n\n稠密检索通过向量相似度召回语义相关内容。\n",
        encoding="utf-8",
    )
    provider = DeterministicProvider(completion_text="这段回答不应该生成。[S1]")
    gateway = ModelGateway(
        provider,
        embedding_dimensions=1024,
        audit_sink=SqlLlmCallAudit(db_session),
    )
    await ingest_markdown_file(
        db_session,
        gateway,
        path=Path("dense.md"),
        library_root=library,
    )

    result = await answer_with_citations(
        db_session,
        gateway,
        query="如何制作提拉米苏?",
        top_k=1,
        refusal_threshold=1.0,
        query_decomposition_enabled=False,
    )
    await db_session.commit()

    assert result.refused is True
    assert result.refusal_reason == "below_threshold"
    assert result.citations == []
    assert result.model is None
    assert result.provider is None
    assert result.top_score is not None and result.top_score < 1.0
    assert result.threshold == 1.0
    assert provider.last_messages == []
    task_types = (
        await db_session.execute(text("SELECT task_type FROM llm_calls ORDER BY created_at"))
    ).scalars()
    assert set(task_types) == {"document_embedding", "query_embedding"}


@pytest.mark.integration
async def test_semantically_insufficient_evidence_refuses_before_answer_generation(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    (library / "benchmark.md").write_text(
        "# 实验\n\n系统在数据集 A 上的 Accuracy 是 80%。\n",
        encoding="utf-8",
    )
    provider = DeterministicProvider(
        completion_text=(
            '{"sufficient":false,"reason":"证据没有 ROUGE-L",'
            '"support_ids":["S1"],"missing_aspects":["ROUGE-L"]}'
        )
    )
    gateway = ModelGateway(
        provider,
        embedding_dimensions=1024,
        audit_sink=SqlLlmCallAudit(db_session),
    )
    await ingest_markdown_file(
        db_session,
        gateway,
        path=Path("benchmark.md"),
        library_root=library,
    )

    result = await answer_with_citations(
        db_session,
        gateway,
        query="系统在数据集 A 上的 ROUGE-L 是多少?",
        query_decomposition_enabled=False,
        top_k=1,
    )
    await db_session.commit()

    assert result.refused is True
    assert result.refusal_reason == "model_insufficient_evidence"
    assert result.evidence_sufficient is False
    assert result.evidence_reason == "证据没有 ROUGE-L"
    assert result.model is None
    task_types = (
        await db_session.execute(text("SELECT task_type FROM llm_calls ORDER BY created_at"))
    ).scalars()
    assert set(task_types) == {
        "document_embedding",
        "query_embedding",
        "evidence_sufficiency",
    }
