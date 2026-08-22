"""Provider profile、会话选择与网关构造。

Profile 存 `<cowork_data_path>/provider_profiles.json`（0600），不进数据库。
参照 openworker `coworker/secrets.py`：本机单用户的控制面配置就该是一个用户自己
能看见、能备份、能手工改的文件。**但密钥不照抄它的明文存法**——api_key 仍然由
`LocalSecretStore` 加密，文件里只有密文，主密钥在另一个文件里。openworker 只靠
文件权限保护明文 key，我们已经有主密钥了，降级过去是白扔一层。

写是全量重写 + `os.replace`。Profile 是个位数到几十条的量级，读一次全进内存、
改完整份落盘，不会出现半截状态。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from uuid6 import uuid7

from app.core.config import Settings
from app.core.db import DbSession as AsyncSession
from app.core.db import SessionFactory
from app.core.private_json import read_private_json, write_private_json
from app.llm_bootstrap import build_custom_model_gateway, build_model_gateway
from app.security.secret_store import LocalSecretStore
from app.telemetry import default_telemetry_store
from app.telemetry.model_budget import build_cost_guard
from workpilot_ai.gateway import ModelGateway
from workpilot_ai.provider_factory import ChatProviderConfig, ProviderKind, build_chat_provider

_STORE_FILE = "provider_profiles.json"
_PROVIDER_KINDS: frozenset[str] = frozenset(
    {
        "openai",
        "anthropic",
        "gemini",
        "deepseek",
        "qwen",
        "ollama",
        "openai_compatible",
    }
)
_lock = threading.Lock()


@dataclass(frozen=True)
class ProviderProfileRecord:
    id: UUID
    name: str
    provider: ProviderKind
    base_url: str
    default_model: str
    api_key_ciphertext: str | None
    context_window_tokens: int
    enabled: bool
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key_ciphertext)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "base_url": self.base_url,
            "default_model": self.default_model,
            "context_window_tokens": self.context_window_tokens,
            "enabled": self.enabled,
            "has_api_key": self.has_api_key,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ProviderNameTakenError(ValueError):
    """名称唯一由这里保证——原来那条 `lower(name)` 唯一索引没有了。"""


def store_path(settings: Settings) -> Path:
    return settings.cowork_data_path.expanduser() / _STORE_FILE


def _now() -> datetime:
    return datetime.now(UTC)


def _read_all(path: Path) -> dict[str, dict[str, Any]]:
    raw = read_private_json(path).get("profiles")
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if isinstance(value, dict)}


def _write_all(path: Path, profiles: dict[str, dict[str, Any]]) -> None:
    write_private_json(path, {"version": 1, "profiles": profiles})


def _record(profile_id: str, value: dict[str, Any]) -> ProviderProfileRecord | None:
    """读不出来就跳过，不抛。

    这个文件用户会亲手编辑（这正是搬出数据库的意义之一），一条写坏了不该让整个
    Provider 列表打不开——那样他连改回去的入口都没有。
    """

    try:
        provider = str(value["provider"])
        if provider not in _PROVIDER_KINDS:
            return None
        ciphertext = value.get("api_key_ciphertext")
        return ProviderProfileRecord(
            id=UUID(profile_id),
            name=str(value["name"]),
            provider=provider,  # type: ignore[arg-type]
            base_url=str(value["base_url"]),
            default_model=str(value["default_model"]),
            api_key_ciphertext=str(ciphertext) if ciphertext else None,
            context_window_tokens=int(value["context_window_tokens"]),
            enabled=bool(value.get("enabled", True)),
            metadata=dict(value.get("metadata") or {}),
            created_at=datetime.fromisoformat(str(value["created_at"])),
            updated_at=datetime.fromisoformat(str(value["updated_at"])),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _serialize(record: ProviderProfileRecord) -> dict[str, Any]:
    return {
        "name": record.name,
        "provider": record.provider,
        "base_url": record.base_url,
        "default_model": record.default_model,
        "api_key_ciphertext": record.api_key_ciphertext,
        "context_window_tokens": record.context_window_tokens,
        "enabled": record.enabled,
        "metadata": record.metadata,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }


def list_provider_profiles(settings: Settings) -> list[ProviderProfileRecord]:
    path = store_path(settings)
    records = [
        record
        for profile_id, value in _read_all(path).items()
        if (record := _record(profile_id, value)) is not None
    ]
    records.sort(key=lambda item: (item.name, item.id.hex))
    return records


def get_provider_profile(settings: Settings, profile_id: UUID) -> ProviderProfileRecord | None:
    value = _read_all(store_path(settings)).get(str(profile_id))
    return None if value is None else _record(str(profile_id), value)


def create_provider_profile(
    settings: Settings,
    *,
    name: str,
    provider: ProviderKind,
    base_url: str,
    default_model: str,
    api_key: str | None,
    context_window_tokens: int,
    enabled: bool,
    metadata: dict[str, Any],
    secret_store: LocalSecretStore,
) -> ProviderProfileRecord:
    path = store_path(settings)
    now = _now()
    record = ProviderProfileRecord(
        id=uuid7(),
        name=name.strip(),
        provider=provider,
        base_url=base_url.rstrip("/"),
        default_model=default_model.strip(),
        api_key_ciphertext=(
            secret_store.encrypt({"api_key": api_key.strip()})
            if api_key and api_key.strip()
            else None
        ),
        context_window_tokens=context_window_tokens,
        enabled=enabled,
        metadata=metadata,
        created_at=now,
        updated_at=now,
    )
    with _lock:
        profiles = _read_all(path)
        _reject_duplicate_name(profiles, name=record.name, keep=None)
        profiles[str(record.id)] = _serialize(record)
        _write_all(path, profiles)
    return record


def update_provider_profile(
    settings: Settings,
    *,
    profile_id: UUID,
    changes: dict[str, Any],
    secret_store: LocalSecretStore,
) -> ProviderProfileRecord | None:
    path = store_path(settings)
    with _lock:
        profiles = _read_all(path)
        value = profiles.get(str(profile_id))
        current = None if value is None else _record(str(profile_id), value)
        if current is None:
            return None
        fields: dict[str, Any] = {
            "name": current.name,
            "base_url": current.base_url,
            "default_model": current.default_model,
            "context_window_tokens": current.context_window_tokens,
            "enabled": current.enabled,
            "metadata": current.metadata,
            "ciphertext": current.api_key_ciphertext,
        }
        for key in (
            "name",
            "base_url",
            "default_model",
            "context_window_tokens",
            "enabled",
            "metadata",
        ):
            if key in changes and changes[key] is not None:
                fields[key] = changes[key]
        if changes.get("clear_api_key"):
            fields["ciphertext"] = None
        elif changes.get("api_key") is not None:
            key = str(changes["api_key"]).strip()
            fields["ciphertext"] = secret_store.encrypt({"api_key": key}) if key else None
        name = str(fields["name"]).strip()
        _reject_duplicate_name(profiles, name=name, keep=str(profile_id))
        updated = ProviderProfileRecord(
            id=current.id,
            name=name,
            provider=current.provider,
            base_url=str(fields["base_url"]).rstrip("/"),
            default_model=str(fields["default_model"]).strip(),
            api_key_ciphertext=fields["ciphertext"],
            context_window_tokens=int(fields["context_window_tokens"]),
            enabled=bool(fields["enabled"]),
            metadata=dict(fields["metadata"]),
            created_at=current.created_at,
            updated_at=_now(),
        )
        profiles[str(profile_id)] = _serialize(updated)
        _write_all(path, profiles)
    return updated


def delete_provider_profile(settings: Settings, profile_id: UUID) -> bool:
    path = store_path(settings)
    with _lock:
        profiles = _read_all(path)
        if profiles.pop(str(profile_id), None) is None:
            return False
        _write_all(path, profiles)
    return True


def _reject_duplicate_name(
    profiles: dict[str, dict[str, Any]], *, name: str, keep: str | None
) -> None:
    folded = name.casefold()
    for profile_id, value in profiles.items():
        if profile_id != keep and str(value.get("name", "")).strip().casefold() == folded:
            raise ProviderNameTakenError(f"已存在同名 Provider: {name}")


async def build_conversation_gateway(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    settings: Settings,
    session_factory: SessionFactory,
    run_id: UUID,
) -> ModelGateway:
    """按会话选定的 Provider 造网关；没选就用默认路由。

    Profile 出了数据库之后这里少了两条 `LEFT JOIN provider_profiles`——会话记的
    只是一个 id，解引用在内存里做。代价是 id 可能悬空（用户删掉了 profile），
    所以下面按"没选 Provider"处理：回落到默认网关，而不是让这次运行失败。
    """

    from app.cowork_store.routing import cowork_store

    store = cowork_store()
    local_metadata = (
        []
        if store is None
        else await store.list_conversation_metadata(
            conversation_id=conversation_id, archived=None, limit=1
        )
    )
    selection = (
        local_metadata[0]["provider_profile_id"],
        local_metadata[0]["model_override"],
    )
    raw_profile_id, model_override = selection
    profile = (
        None
        if raw_profile_id is None
        else get_provider_profile(settings, UUID(str(raw_profile_id)))
    )
    telemetry = default_telemetry_store()
    audit = telemetry
    budget = build_cost_guard(settings, telemetry)
    # 审计库换成 SQLite 之后没有指向 agent_runs 的外键了，本地 run_id 可以照常写进去。
    if profile is None:
        cowork_settings = settings.model_copy(
            update={"model_timeout_s": settings.cowork_model_timeout_s}
        )
        return build_model_gateway(
            cowork_settings,
            audit_sink=audit,
            budget_guard=budget,
            run_id=run_id,
        )
    if not profile.enabled:
        raise RuntimeError(f"会话选择的 Provider {profile.name} 已停用，请重新选择")
    secrets = LocalSecretStore(settings.secret_store_key_path).decrypt(profile.api_key_ciphertext)
    api_key = str(secrets.get("api_key") or "")
    if profile.provider != "ollama" and not api_key:
        raise RuntimeError(f"Provider {profile.name} 缺少 API Key，请在模型设置中补充")
    model = str(model_override or profile.default_model).strip()
    chat_provider = build_chat_provider(
        ChatProviderConfig(
            provider=profile.provider,
            base_url=profile.base_url,
            api_key=api_key,
            model=model,
            timeout_s=settings.cowork_model_timeout_s,
            prompt_cache_key_supported=(
                settings.openai_compatible_prompt_cache_key_enabled
                and profile.provider == "openai_compatible"
            ),
        ),
        trust_env=settings.model_trust_env,
    )
    return build_custom_model_gateway(
        settings,
        chat_provider=chat_provider,
        context_window_tokens=profile.context_window_tokens,
        audit_sink=audit,
        budget_guard=budget,
        run_id=run_id,
    )


def provider_api_key(profile: ProviderProfileRecord, secret_store: LocalSecretStore) -> str:
    return str(secret_store.decrypt(profile.api_key_ciphertext).get("api_key") or "")
