import httpx
import pytest

from app.api.dependencies import require_owner_identity
from app.main import create_app

pytestmark = pytest.mark.usefixtures("local_cowork_store")


def _test_app(*, owner: bool):
    # 记忆已经不读数据库了：面板走的就是 Cowork 的那份 SQLite 存储。
    app = create_app()
    if owner:
        app.dependency_overrides[require_owner_identity] = lambda: None
    return app


async def test_memory_api_is_owner_only() -> None:
    transport = httpx.ASGITransport(app=_test_app(owner=False))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/memories")

    assert response.status_code == 401
    assert response.json()["detail"] == "需要先登录 owner"


async def test_memory_api_manual_lifecycle_preserves_history() -> None:
    transport = httpx.ASGITransport(app=_test_app(owner=True))
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
