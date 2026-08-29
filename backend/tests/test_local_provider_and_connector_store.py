"""Provider profile 与连接器账户搬进本机 JSON 之后的行为约束。

这些不变量原来是数据库替我们守的——`lower(name)` 唯一索引、`(kind, lower(name))`
唯一索引、`oauth_states` 主键加一次 DELETE、指向 `provider_profiles` 的外键。
表没了之后它们必须由代码守住，所以这里逐条钉下来。
"""

import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.cowork.connectors import (
    ConnectorNameTakenError,
    connector_secrets,
    create_connector_account,
    delete_connector_account,
    get_connector_account,
    list_connector_accounts,
    set_connector_status,
    update_connector_account,
)
from app.cowork.connectors import store_path as connector_store_path
from app.cowork.oauth_connectors import (
    begin_oauth,
    complete_oauth,
    reject_oauth,
    reset_pending_authorizations,
)
from app.cowork.provider_profiles import (
    ProviderNameTakenError,
    ProviderSelectionRequiredError,
    build_conversation_gateway,
    create_provider_profile,
    default_provider_profile,
    delete_provider_profile,
    get_provider_profile,
    list_provider_profiles,
    update_provider_profile,
)
from app.cowork.provider_profiles import store_path as provider_store_path
from app.security.secret_store import LocalSecretStore


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        cowork_data_path=tmp_path / "state",
        secret_store_key_path=tmp_path / "keys" / "master.key",
    )


@pytest.fixture(autouse=True)
def _clear_pending() -> None:
    reset_pending_authorizations()


def _secret_store(settings: Settings) -> LocalSecretStore:
    return LocalSecretStore(settings.secret_store_key_path)


def _profile(settings: Settings, *, name: str = "本地 DeepSeek", api_key: str | None = "sk-live"):
    return create_provider_profile(
        settings,
        name=name,
        provider="deepseek",
        base_url="https://api.deepseek.com/v1/",
        default_model="deepseek-chat",
        api_key=api_key,
        context_window_tokens=65_536,
        enabled=True,
        metadata={"note": "手工添加"},
        secret_store=_secret_store(settings),
    )


def test_api_key_is_ciphertext_on_disk_and_the_file_is_owner_only(settings: Settings) -> None:
    created = _profile(settings)
    path = provider_store_path(settings)
    raw = path.read_text(encoding="utf-8")

    # 搬到文件不等于降级成明文：密钥仍然过 LocalSecretStore，主密钥在另一个文件里。
    assert "sk-live" not in raw
    assert created.has_api_key is True
    assert "api_key" not in created.public()
    assert json.loads(raw)["profiles"][str(created.id)]["api_key_ciphertext"].startswith("v1:")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert _secret_store(settings).decrypt(created.api_key_ciphertext)["api_key"] == "sk-live"


def test_provider_name_stays_unique_without_the_database_index(settings: Settings) -> None:
    first = _profile(settings, name="本地 DeepSeek")
    with pytest.raises(ProviderNameTakenError):
        _profile(settings, name="本地 deepseek")  # 大小写不敏感，同 lower(name) 唯一索引

    second = _profile(settings, name="另一个")
    with pytest.raises(ProviderNameTakenError):
        update_provider_profile(
            settings,
            profile_id=second.id,
            changes={"name": "本地 DeepSeek"},
            secret_store=_secret_store(settings),
        )
    # 改成自己原来的名字不算冲突。
    assert (
        update_provider_profile(
            settings,
            profile_id=first.id,
            changes={"name": "本地 DeepSeek"},
            secret_store=_secret_store(settings),
        )
        is not None
    )


def test_update_preserves_created_at_and_can_clear_the_key(settings: Settings) -> None:
    created = _profile(settings)
    updated = update_provider_profile(
        settings,
        profile_id=created.id,
        changes={"clear_api_key": True, "base_url": "https://proxy.internal/v1/"},
        secret_store=_secret_store(settings),
    )

    assert updated is not None
    assert updated.created_at == created.created_at
    assert updated.updated_at >= created.updated_at
    assert updated.has_api_key is False
    assert updated.base_url == "https://proxy.internal/v1"  # 结尾斜杠照旧被吃掉
    assert get_provider_profile(settings, created.id) == updated

    assert delete_provider_profile(settings, created.id) is True
    assert delete_provider_profile(settings, created.id) is False
    assert list_provider_profiles(settings) == []


def test_a_corrupt_entry_does_not_take_the_whole_list_down(settings: Settings) -> None:
    good = _profile(settings, name="好的")
    path = provider_store_path(settings)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["profiles"][str(uuid4())] = {"name": "缺字段的", "provider": "deepseek"}
    path.write_text(json.dumps(payload), encoding="utf-8")

    # 这个文件用户会亲手编辑，一条写坏了不该让他连改回去的入口都打不开。
    assert [item.id for item in list_provider_profiles(settings)] == [good.id]


def test_default_provider_prefers_the_configured_qwen_35b_profile(settings: Settings) -> None:
    fallback = _profile(settings, name="A DeepSeek")
    qwen = create_provider_profile(
        settings,
        name="Z Qwen 35B",
        provider="qwen",
        base_url="http://127.0.0.1:8102/v1",
        default_model="qwen3.6-35b-a3b",
        api_key="EMPTY",
        context_window_tokens=102_400,
        enabled=True,
        metadata={},
        secret_store=_secret_store(settings),
    )

    selected = default_provider_profile(
        settings.model_copy(update={"tier_main_model": "qwen3.6-35b-a3b"})
    )

    assert selected is not None
    assert selected.id == qwen.id
    assert selected.id != fallback.id


async def test_unbound_conversation_is_persistently_bound_before_gateway_build(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = settings.model_copy(update={"tier_main_model": "qwen3.6-35b-a3b"})
    qwen = create_provider_profile(
        configured,
        name="Qwen 35B",
        provider="qwen",
        base_url="http://127.0.0.1:8102/v1",
        default_model="qwen3.6-35b-a3b",
        api_key="EMPTY",
        context_window_tokens=102_400,
        enabled=True,
        metadata={},
        secret_store=_secret_store(configured),
    )
    metadata: dict[str, object] = {
        "provider_profile_id": None,
        "model_override": None,
        "unattended": True,
        "approval_mode": "auto",
        "persona_name": "general",
    }
    updates: list[dict[str, object]] = []

    class LocalStore:
        async def list_conversation_metadata(self, **_: object) -> list[dict[str, object]]:
            return [metadata]

        async def update_conversation_runtime(self, **kwargs: object) -> bool:
            updates.append(kwargs)
            metadata.update(
                provider_profile_id=str(kwargs["provider_profile_id"]),
                model_override=kwargs["model_override"],
            )
            return True

    captured: dict[str, object] = {}

    def fake_build_custom_model_gateway(*_: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("app.cowork_store.routing.cowork_store", lambda: LocalStore())
    monkeypatch.setattr(
        "app.cowork.provider_profiles.build_custom_model_gateway",
        fake_build_custom_model_gateway,
    )
    conversation_id = uuid4()

    await build_conversation_gateway(
        AsyncMock(),
        conversation_id=conversation_id,
        settings=configured,
        session_factory=AsyncMock(),
        run_id=uuid4(),
    )

    assert updates == [
        {
            "conversation_id": conversation_id,
            "provider_profile_id": qwen.id,
            "model_override": "qwen3.6-35b-a3b",
            "unattended": True,
            "approval_mode": "auto",
            "persona_name": "general",
        }
    ]
    chat_provider = captured["chat_provider"]
    assert hasattr(chat_provider, "chat_model")
    assert chat_provider.chat_model == "qwen3.6-35b-a3b"


async def test_selected_profile_gateway_keeps_the_local_run_id(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(settings)

    class LocalStore:
        async def list_conversation_metadata(self, **_: object) -> list[dict[str, object]]:
            return [{"provider_profile_id": str(profile.id), "model_override": None}]

    captured: dict[str, object] = {}

    def fake_build_custom_model_gateway(*_: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("app.cowork_store.routing.cowork_store", lambda: LocalStore())
    monkeypatch.setattr(
        "app.cowork.provider_profiles.build_custom_model_gateway",
        fake_build_custom_model_gateway,
    )
    run_id = uuid4()
    await build_conversation_gateway(
        AsyncMock(),
        conversation_id=uuid4(),
        settings=settings,
        session_factory=AsyncMock(),
        run_id=run_id,
    )

    assert captured["run_id"] == run_id


async def test_dangling_profile_id_requires_a_new_selection(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """外键没了，会话可能指着一个已删除的 profile。"""

    deleted_id = _profile(settings).id
    assert delete_provider_profile(settings, deleted_id) is True

    class LocalStore:
        async def list_conversation_metadata(self, **_: object) -> list[dict[str, object]]:
            return [{"provider_profile_id": str(deleted_id), "model_override": None}]

    monkeypatch.setattr("app.cowork_store.routing.cowork_store", lambda: LocalStore())
    with pytest.raises(ProviderSelectionRequiredError, match="已被删除"):
        await build_conversation_gateway(
            AsyncMock(),
            conversation_id=uuid4(),
            settings=settings,
            session_factory=AsyncMock(),
            run_id=uuid4(),
        )


def _account(settings: Settings, *, name: str = "主账号", kind: str = "github"):
    return create_connector_account(
        settings,
        kind=kind,  # type: ignore[arg-type]
        name=name,
        auth_type="oauth2",
        client_id="client-123",
        client_secret="shhh",
        access_token=None,
        refresh_token=None,
        redirect_uri="http://127.0.0.1:8000/api/v1/connectors/oauth/callback",
        scopes=["read:user"],
        config={},
        enabled=True,
        secret_store=_secret_store(settings),
    )


def test_connector_secrets_are_ciphertext_and_name_is_unique_per_kind(
    settings: Settings,
) -> None:
    created = _account(settings)
    raw = connector_store_path(settings).read_text(encoding="utf-8")

    assert "shhh" not in raw
    assert created.public()["has_secrets"] is True
    assert "secret_ciphertext" not in created.public()
    assert connector_secrets(created, _secret_store(settings))["client_secret"] == "shhh"

    with pytest.raises(ConnectorNameTakenError):
        _account(settings, name="主账号")
    # 唯一性按 (kind, name)：不同连接器可以同名，同原来那条复合唯一索引。
    assert _account(settings, name="主账号", kind="feishu") is not None
    assert len(list_connector_accounts(settings)) == 2


def test_status_update_keeps_untouched_fields_and_clear_secrets_resets_status(
    settings: Settings,
) -> None:
    account = _account(settings)
    set_connector_status(
        settings,
        account_id=account.id,
        status="connected",
        secret_ciphertext=_secret_store(settings).encrypt({"access_token": "tok"}),
        external_account_id="42",
        external_account_name="octocat",
        expires_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    # 没传的字段保持原值，对应原来那几个 COALESCE(:x, x)。
    set_connector_status(settings, account_id=account.id, status="error", error="令牌失效")

    refreshed = get_connector_account(settings, account.id)
    assert refreshed is not None
    assert refreshed.external_account_name == "octocat"
    assert refreshed.status == "error"
    assert refreshed.last_error == "令牌失效"
    assert refreshed.last_checked_at is not None

    cleared = update_connector_account(
        settings,
        account_id=account.id,
        changes={"clear_secrets": True},
        secret_store=_secret_store(settings),
    )
    assert cleared is not None
    assert cleared.status == "configured"
    assert cleared.secret_ciphertext is None
    assert delete_connector_account(settings, account.id) is True


async def test_oauth_state_is_one_shot_and_a_new_flow_invalidates_the_old(
    settings: Settings,
) -> None:
    account = _account(settings)
    first = await begin_oauth(settings=settings, account=account, redirect_uri=None)
    assert "state=" in first.authorization_url
    assert get_connector_account(settings, account.id).status == "authorizing"  # type: ignore[union-attr]

    # 同一账户重新发起授权作废上一条：旧标签页再回来也换不到 token。
    second = await begin_oauth(settings=settings, account=account, redirect_uri=None)
    assert second.state != first.state
    with pytest.raises(ValueError, match="无效或已使用"):
        await complete_oauth(
            settings=settings,
            state=first.state,
            code="code",
            secret_store=_secret_store(settings),
            timeout_s=1.0,
            trust_env=False,
        )

    # 未知 state 同样拒绝，且不能因为"表里查不到"就当成通过。
    with pytest.raises(ValueError, match="无效或已使用"):
        await complete_oauth(
            settings=settings,
            state="x" * 48,
            code="code",
            secret_store=_secret_store(settings),
            timeout_s=1.0,
            trust_env=False,
        )


async def test_expired_oauth_state_is_swept_and_rejected(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = _account(settings)
    started = await begin_oauth(settings=settings, account=account, redirect_uri=None)

    import app.cowork.oauth_connectors as module

    pending = module._pending[started.state]
    module._pending[started.state] = module._PendingAuthorization(
        account_id=pending.account_id,
        redirect_uri=pending.redirect_uri,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    with pytest.raises(ValueError, match="无效或已使用"):
        await complete_oauth(
            settings=settings,
            state=started.state,
            code="code",
            secret_store=_secret_store(settings),
            timeout_s=1.0,
            trust_env=False,
        )
    assert module._pending == {}


async def test_rejected_oauth_becomes_a_visible_terminal_state(settings: Settings) -> None:
    account = _account(settings)
    started = await begin_oauth(settings=settings, account=account, redirect_uri=None)

    assert reject_oauth(settings=settings, state=started.state, reason="access_denied") is True
    rejected = get_connector_account(settings, account.id)
    assert rejected is not None
    assert rejected.status == "error"
    assert rejected.last_error == "OAuth 授权未完成：access_denied"
    assert reject_oauth(settings=settings, state=started.state, reason="replay") is False
