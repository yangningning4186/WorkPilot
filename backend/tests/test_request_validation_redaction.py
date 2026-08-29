"""Request validation must never echo secret-bearing control-plane bodies."""

import httpx
import pytest

from app.api.dependencies import require_owner_identity
from app.main import create_app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/api/v1/providers",
            {
                "name": "unsafe-provider",
                "provider": "openai_compatible",
                "base_url": "https://user:private-validation-secret@example.test/v1",
                "default_model": "example",
                "api_key": "private-validation-secret",
            },
        ),
        (
            "/api/v1/connectors",
            {
                "kind": "feishu",
                "name": "unsafe-connector",
                "auth_type": "token",
                "access_token": "private-validation-secret",
                "redirect_uri": "https://user:private-validation-secret@example.test/callback",
            },
        ),
    ],
)
async def test_secret_bearing_validation_errors_never_reflect_request_input(
    path: str,
    payload: dict[str, object],
) -> None:
    app = create_app()
    app.dependency_overrides[require_owner_identity] = lambda: None

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(path, json=payload)

    assert response.status_code == 422
    assert response.json() == {"detail": "请求参数无效"}
    assert "private-validation-secret" not in response.text


@pytest.mark.asyncio
async def test_malformed_json_validation_never_reflects_body() -> None:
    app = create_app()
    app.dependency_overrides[require_owner_identity] = lambda: None
    secret = "private-malformed-validation-secret"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/providers",
            content=f'{{"api_key":"{secret}"',
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "请求参数无效"}
    assert secret not in response.text
