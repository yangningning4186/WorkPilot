import httpx

from app.api.dependencies import require_admin_session
from app.core.config import Settings, get_settings
from app.main import create_app


async def test_local_annotation_page_and_assets_are_available() -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        app_env="test", annotation_tool_enabled=True
    )
    app.dependency_overrides[require_admin_session] = lambda: None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/annotation")
        script = await client.get("/annotation/assets/app.js")

    assert page.status_code == 200
    assert "Gold Span Lab" in page.text
    assert script.status_code == 200
    assert "utf16_start" in script.text
    assert 'value="temporal"' in page.text
    assert 'value="global"' in page.text
    assert 'value="agent_task"' in page.text
    assert "gold_tools" in script.text
    assert "temporal_ctx" in script.text


async def test_annotation_page_is_disabled_in_production() -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        app_env="production",
        annotation_tool_enabled=True,
        ip_rate_limit_enabled=False,
    )
    app.dependency_overrides[require_admin_session] = lambda: None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/annotation")

    assert response.status_code == 404
