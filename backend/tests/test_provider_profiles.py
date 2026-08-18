from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.llm.provider_factory import ChatProviderConfig, build_chat_provider
from app.security.secret_store import LocalSecretStore, SecretStoreError
from app.services.provider_probe import probe_provider_profile
from app.services.provider_profiles import ProviderProfileRecord


def test_secret_store_encrypts_and_reuses_master_key(tmp_path: Path) -> None:
    key_path = tmp_path / "keys" / "master.key"
    first = LocalSecretStore(key_path)
    ciphertext = first.encrypt({"api_key": "sk-secret", "refresh_token": "refresh"})

    assert "sk-secret" not in ciphertext
    assert LocalSecretStore(key_path).decrypt(ciphertext) == {
        "api_key": "sk-secret",
        "refresh_token": "refresh",
    }
    assert key_path.stat().st_mode & 0o777 == 0o600


def test_secret_store_rejects_unknown_ciphertext_version(tmp_path: Path) -> None:
    store = LocalSecretStore(tmp_path / "master.key")

    with pytest.raises(SecretStoreError, match="版本"):
        store.decrypt("v2:not-supported")


def test_provider_factory_keeps_provider_identity() -> None:
    provider = build_chat_provider(
        ChatProviderConfig(
            provider="deepseek",
            base_url="https://api.deepseek.com/v1",
            api_key="test",
            model="deepseek-chat",
            timeout_s=10,
        )
    )

    assert provider.name == "deepseek"


@pytest.mark.asyncio
async def test_probe_gemini_model_catalog() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"models": [{"name": "models/gemini-2.5-pro"}, {"name": "models/gemini-2.5-flash"}]},
        )

    async with httpx.AsyncClient(
        base_url="https://generativelanguage.googleapis.com/v1beta/",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await probe_provider_profile(
            ProviderProfileRecord(
                id=uuid4(),
                name="Gemini",
                provider="gemini",
                base_url="https://generativelanguage.googleapis.com/v1beta",
                default_model="gemini-2.5-pro",
                api_key_ciphertext=None,
                context_window_tokens=1_000_000,
                enabled=True,
                metadata={},
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            ),
            api_key="gemini-key",
            timeout_s=10,
            trust_env=False,
            client=client,
        )

    assert result.models == ["gemini-2.5-flash", "gemini-2.5-pro"]
    assert requests[0].headers["x-goog-api-key"] == "gemini-key"
    assert requests[0].url.path.endswith("/models")
