from dataclasses import dataclass

from fastapi.testclient import TestClient

from reranker_service.main import create_app


@dataclass
class FakeScorer:
    model_name: str = "test-reranker"
    revision: str = "test-revision"
    device: str = "cpu"
    dtype: str = "float32"
    batch_size: int = 2
    max_length: int = 512

    def score(self, query: str, documents: list[str]) -> list[float]:
        del query
        return [0.9 if "目标" in document else 0.1 for document in documents]


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
    }


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
