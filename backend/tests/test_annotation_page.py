import httpx

from app.core.config import Settings, get_settings
from app.main import create_app


async def test_local_annotation_page_and_assets_are_available() -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        app_env="test", annotation_tool_enabled=True
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/annotation")
        script = await client.get("/annotation/assets/app.js")

    assert page.status_code == 200
    assert "Gold Span Lab" in page.text
    assert script.status_code == 200
    assert "utf16_start" in script.text


async def test_annotation_page_is_disabled_in_production() -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        app_env="production", annotation_tool_enabled=True
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/annotation")

    assert response.status_code == 404
