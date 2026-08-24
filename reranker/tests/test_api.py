import threading
from dataclasses import dataclass

from fastapi.testclient import TestClient

from reranker_service.main import Document, TokenSpanAudit, _token_span_coverage, create_app


@dataclass
class FakeScorer:
    model_name: str = "test-reranker"
    revision: str = "test-revision"
    device: str = "cpu"
    dtype: str = "float32"
    batch_size: int = 2
    max_length: int = 512

    def score(
        self, query: str, documents: list[str], *, max_length: int | None = None
    ) -> list[float]:
        del query
        del max_length
        return [0.9 if "目标" in document else 0.1 for document in documents]

    def audit_spans(
        self,
        query: str,
        documents: list[Document],
        *,
        max_length: int,
    ) -> list[TokenSpanAudit]:
        del query
        return [
            TokenSpanAudit(
                document_id=document.id,
                span_id=span.id,
                char_start=span.char_start,
                char_end=span.char_end,
                total_tokens=1,
                visible_tokens=int(span.char_end <= max_length),
                fully_visible=span.char_end <= max_length,
            )
            for document in documents
            for span in document.audit_spans
        ]


@dataclass
class ThreadRecordingScorer(FakeScorer):
    score_thread_id: int | None = None

    def score(
        self, query: str, documents: list[str], *, max_length: int | None = None
    ) -> list[float]:
        self.score_thread_id = threading.get_ident()
        return super().score(query, documents, max_length=max_length)


def test_health_and_rerank_contract() -> None:
    with TestClient(create_app(FakeScorer())) as client:
        health = client.get("/health")
        response = client.post(
            "/v1/rerank",
            json={
                "model": "test-reranker",
                "query": "目标是什么",
                "documents": [
                    {"id": "C1", "text": "无关内容"},
                    {"id": "C2", "text": "这里包含目标"},
                ],
                "top_n": 2,
            },
        )

    assert health.json()["status"] == "ok"
    assert response.status_code == 200
    assert response.json() == {
        "model": "test-reranker",
        "results": [
            {"index": 1, "id": "C2", "relevance_score": 0.9},
            {"index": 0, "id": "C1", "relevance_score": 0.1},
        ],
        "span_audits": [],
    }


def test_inference_does_not_run_on_the_asgi_event_loop_thread() -> None:
    scorer = ThreadRecordingScorer()
    application = create_app(scorer)

    @application.middleware("http")
    async def remember_event_loop_thread(request, call_next):
        request.app.state.event_loop_thread_id = threading.get_ident()
        return await call_next(request)

    with TestClient(application) as client:
        response = client.post(
            "/v1/rerank",
            json={
                "query": "目标是什么",
                "documents": [{"id": "C1", "text": "这里包含目标"}],
                "top_n": 1,
            },
        )

    assert response.status_code == 200
    assert scorer.score_thread_id is not None
    assert scorer.score_thread_id != application.state.event_loop_thread_id


def test_rerank_rejects_duplicate_ids_and_model_mismatch() -> None:
    with TestClient(create_app(FakeScorer())) as client:
        duplicate = client.post(
            "/v1/rerank",
            json={
                "query": "q",
                "documents": [{"id": "C1", "text": "a"}, {"id": "C1", "text": "b"}],
                "top_n": 1,
            },
        )
        mismatch = client.post(
            "/v1/rerank",
            json={
                "model": "other",
                "query": "q",
                "documents": [{"id": "C1", "text": "a"}],
                "top_n": 1,
            },
        )

    assert duplicate.status_code == 422
    assert mismatch.status_code == 409


def test_request_max_length_and_token_span_audit() -> None:
    with TestClient(create_app(FakeScorer())) as client:
        response = client.post(
            "/v1/rerank",
            json={
                "query": "q",
                "documents": [
                    {
                        "id": "C1",
                        "text": "x" * 300,
                        "audit_spans": [{"id": "gold-1", "char_start": 200, "char_end": 260}],
                    }
                ],
                "top_n": 1,
                "max_length": 256,
            },
        )
        too_large = client.post(
            "/v1/rerank",
            json={
                "query": "q",
                "documents": [{"id": "C1", "text": "text"}],
                "top_n": 1,
                "max_length": 1024,
            },
        )

    assert response.status_code == 200
    assert response.json()["span_audits"] == [
        {
            "document_id": "C1",
            "span_id": "gold-1",
            "char_start": 200,
            "char_end": 260,
            "total_tokens": 1,
            "visible_tokens": 0,
            "fully_visible": False,
        }
    ]
    assert too_large.status_code == 422


def test_token_span_coverage_requires_every_overlapping_token() -> None:
    assert _token_span_coverage(
        [(0, 2), (2, 4), (4, 6)],
        [(0, 2), (2, 4)],
        char_start=1,
        char_end=5,
    ) == (3, 2, False)
    assert _token_span_coverage(
        [(0, 2), (2, 4), (4, 6)],
        [(0, 2), (2, 4), (4, 6)],
        char_start=1,
        char_end=5,
    ) == (3, 3, True)
