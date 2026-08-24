import httpx
import pytest
from pydantic import ValidationError

from app.api.dependencies import require_admin_session, require_owner_identity
from app.core.config import Settings
from app.core.db import DbSession as AsyncSession
from app.core.db import get_db_session
from app.core.desktop_security import DESKTOP_LAUNCH_TOKEN_HEADER
from app.main import create_app


def test_desktop_mode_requires_strong_launch_token() -> None:
    with pytest.raises(ValidationError, match="至少 32 字符"):
        Settings(desktop_mode_enabled=True, desktop_launch_token="short")


async def test_desktop_mode_rejects_requests_without_current_launch_token() -> None:
    token = "desktop-launch-token-" + "x" * 32
    app = create_app(
        Settings(
            app_env="test",
            desktop_mode_enabled=True,
            desktop_launch_token=token,
        )
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.get("/health/live")
        invalid = await client.get("/health/live", headers={DESKTOP_LAUNCH_TOKEN_HEADER: "wrong"})
        preflight = await client.options(
            "/api/v1/conversations",
            headers={
                "Origin": "tauri://localhost",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": DESKTOP_LAUNCH_TOKEN_HEADER,
            },
        )
        dev_preflight = await client.options(
            "/api/v1/conversations",
            headers={
                "Origin": "http://127.0.0.1:3000",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": DESKTOP_LAUNCH_TOKEN_HEADER,
            },
        )
        accepted = await client.get("/health/live", headers={DESKTOP_LAUNCH_TOKEN_HEADER: token})

    assert missing.status_code == invalid.status_code == 401
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "tauri://localhost"
    assert dev_preflight.status_code == 200
    assert dev_preflight.headers["access-control-allow-origin"] == "http://127.0.0.1:3000"
    assert accepted.status_code == 200
    assert accepted.json() == {"status": "ok"}


async def test_desktop_launch_token_is_the_local_owner_session(
    db_session: AsyncSession,
) -> None:
    token = "desktop-launch-token-" + "x" * 32
    app = create_app(
        Settings(
            app_env="test",
            desktop_mode_enabled=True,
            desktop_launch_token=token,
        )
    )

    async def override_session():  # type: ignore[no-untyped-def]
        yield db_session

    app.dependency_overrides[get_db_session] = override_session
    transport = httpx.ASGITransport(app=app)
    headers = {DESKTOP_LAUNCH_TOKEN_HEADER: token}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        authenticated = await client.get("/api/v1/auth/admin/session", headers=headers)
        conversations = await client.get("/api/v1/conversations", headers=headers)

    assert authenticated.status_code == 200
    assert conversations.status_code == 200
    app.dependency_overrides.pop(get_db_session, None)
    app.dependency_overrides.pop(require_owner_identity, None)
    app.dependency_overrides.pop(require_admin_session, None)
