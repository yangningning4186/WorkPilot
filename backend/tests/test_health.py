import httpx
import pytest

from app.main import create_app


def test_standalone_editor_routes_are_not_registered() -> None:
    paths = {getattr(route, "path", "") for route in create_app().routes}

    assert not any(path.startswith("/api/v1/editor") for path in paths)


async def test_live_returns_trace_id() -> None:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-trace-id"]


async def test_valid_incoming_trace_id_is_preserved() -> None:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/live", headers={"x-trace-id": "test-trace-1"})

    assert response.headers["x-trace-id"] == "test-trace-1"


@pytest.mark.integration
async def test_ready_checks_the_database() -> None:
    """Redis 已退役，就绪探针只剩数据库一个依赖。"""

    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
