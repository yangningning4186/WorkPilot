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
from app.retrieval.dense import DenseSearchHit
from app.services.grounded_answer import answer_with_citations, evaluate_refusal
from app.services.markdown_ingestion import ingest_markdown_file
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

    assert evaluate_refusal([], threshold=0.5) == (None, "no_evidence")
    assert evaluate_refusal([hit], threshold=0.5) == (0.5, None)
    assert evaluate_refusal([hit], threshold=0.5001) == (0.5, "below_threshold")

    with pytest.raises(ValueError, match="-1 到 1"):
        evaluate_refusal([hit], threshold=1.1)


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
    provider = DeterministicProvider(completion_text="稠密检索使用向量相似度召回语义相关内容。[S1]")
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
    )
    await db_session.commit()

    assert result.refused is False
    assert result.refusal_reason is None
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

    async def override_session():
        yield db_session

    async def override_gateway():
        yield gateway

    app = create_app()
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_model_gateway] = override_gateway
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

        provider.completion_text = "这是一个带伪引用的回答。[S99]"
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
    assert set(task_types) == {"document_embedding", "query_embedding", "grounded_answer"}


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
