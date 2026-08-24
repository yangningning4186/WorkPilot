import httpx

from app.api.dependencies import get_admin_session_store
from app.core.config import Settings, get_settings
from app.main import create_app
from app.platform.admin_sessions import hash_admin_password


class MemoryAdminSessionStore:
    def __init__(self) -> None:
        self.tokens: set[str] = set()

    async def issue(self) -> str:
        token = "admin-test-token"
        self.tokens.add(token)
        return token

    async def validate(self, token: str) -> bool:
        return token in self.tokens

    async def revoke(self, token: str) -> None:
        self.tokens.discard(token)


def _app(store: MemoryAdminSessionStore, *, password: str = "correct horse"):
    app = create_app()
    settings = Settings(
        app_env="test",
        demo_admin_password_hash=hash_admin_password(password),
        admin_session_ttl_s=3600,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_admin_session_store] = lambda: store
    return app


async def test_admin_login_session_and_logout_use_http_only_cookie() -> None:
    store = MemoryAdminSessionStore()
    transport = httpx.ASGITransport(app=_app(store))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post("/api/v1/auth/admin/login", json={"password": "correct horse"})
        assert login.status_code == 200
        cookie = login.headers["set-cookie"].lower()
        assert "workpilot_admin_session=" in cookie
        assert "httponly" in cookie
        assert "samesite=lax" in cookie
        assert "max-age=3600" in cookie
        assert (await client.get("/api/v1/auth/admin/session")).status_code == 200

        logout = await client.post("/api/v1/auth/admin/logout")
        assert logout.status_code == 204
        assert "max-age=0" in logout.headers["set-cookie"].lower()
        assert (await client.get("/api/v1/auth/admin/session")).status_code == 401


async def test_admin_login_rejects_bad_password_and_missing_configuration() -> None:
    store = MemoryAdminSessionStore()
    app = _app(store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.post("/api/v1/auth/admin/login", json={"password": "wrong"})
        ).status_code == 401

    unconfigured = create_app()
    unconfigured.dependency_overrides[get_settings] = lambda: Settings(
        app_env="test", demo_admin_password_hash=""
    )
    unconfigured.dependency_overrides[get_admin_session_store] = lambda: store
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=unconfigured), base_url="http://test"
    ) as client:
        assert (
            await client.post("/api/v1/auth/admin/login", json={"password": "anything"})
        ).status_code == 503
