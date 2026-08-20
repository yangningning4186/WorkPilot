from collections.abc import AsyncIterator

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_model_gateway, require_owner_identity
from app.core.db import get_db_session
from app.main import create_app
from app.platform.request_identity import RequestIdentity
from tests.fakes import DeterministicProvider
from workpilot_ai.gateway import ModelGateway


def _test_app(db_session: AsyncSession, *, owner: bool):
    async def override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def override_gateway() -> AsyncIterator[ModelGateway]:
        gateway = ModelGateway(
            DeterministicProvider(),
            embedding_dimensions=1024,
            embedding_revision="memory-api-test",
        )
        yield gateway

    app = create_app()
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_model_gateway] = override_gateway
    if owner:
        app.dependency_overrides[require_owner_identity] = lambda: RequestIdentity(
            scope="local_owner"
        )
    return app


async def test_memory_api_is_owner_only(db_session: AsyncSession) -> None:
    transport = httpx.ASGITransport(app=_test_app(db_session, owner=False))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/memories")

    assert response.status_code == 401
    assert response.json()["detail"] == "需要先登录 owner"


async def test_memory_api_manual_lifecycle_preserves_history(
    db_session: AsyncSession,
) -> None:
    transport = httpx.ASGITransport(app=_test_app(db_session, owner=True))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/api/v1/memories",
            json={
                "category": "preference",
                "fact": "偏好简洁回答",
                "pinned": True,
            },
        )
        assert created.status_code == 201
        original = created.json()

        edited = await client.patch(
            f"/api/v1/memories/{original['id']}",
            json={"fact": "偏好先给结论", "pinned": False},
        )
        assert edited.status_code == 200
        current = edited.json()
        assert current["id"] != original["id"]
        assert current["fact"] == "偏好先给结论"
        assert current["source_type"] == "manual"

        history = await client.get("/api/v1/memories", params={"view": "history"})
        assert history.status_code == 200
        assert [item["id"] for item in history.json()["items"]] == [original["id"]]

        deleted = await client.delete(f"/api/v1/memories/{current['id']}")
        assert deleted.status_code == 204
        current_list = await client.get("/api/v1/memories")
        assert current_list.json()["total"] == 0

        restored = await client.post(f"/api/v1/memories/{original['id']}/restore")
        assert restored.status_code == 200
        assert restored.json()["fact"] == original["fact"]
        assert restored.json()["id"] not in {original["id"], current["id"]}
