"""Provider profile、会话选择与网关构造。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from uuid6 import uuid7

from app.core.config import Settings
from app.llm.audit import SqlLlmCallAudit
from app.llm.gateway import ModelGateway, build_custom_model_gateway, build_model_gateway
from app.llm.provider_factory import ChatProviderConfig, ProviderKind, build_chat_provider
from app.security.secret_store import LocalSecretStore
from app.services.model_budget import build_cost_guard


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


_COLUMN_NAMES = (
    "id",
    "name",
    "provider",
    "base_url",
    "default_model",
    "api_key_ciphertext",
    "context_window_tokens",
    "enabled",
    "metadata",
    "created_at",
    "updated_at",
)
_COLUMNS = ", ".join(_COLUMN_NAMES)


def _record(row: Any) -> ProviderProfileRecord:
    value = dict(row)
    value["metadata"] = dict(value.get("metadata") or {})
    return ProviderProfileRecord(**value)


async def list_provider_profiles(session: AsyncSession) -> list[ProviderProfileRecord]:
    rows = (
        (
            await session.execute(
                text(f"SELECT {_COLUMNS} FROM provider_profiles ORDER BY name, id")
            )
        )
        .mappings()
        .all()
    )
    return [_record(row) for row in rows]


async def get_provider_profile(
    session: AsyncSession, profile_id: UUID
) -> ProviderProfileRecord | None:
    row = (
        (
            await session.execute(
                text(f"SELECT {_COLUMNS} FROM provider_profiles WHERE id = :id"),
                {"id": profile_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _record(row)


async def create_provider_profile(
    session: AsyncSession,
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
    profile_id = uuid7()
    ciphertext = (
        secret_store.encrypt({"api_key": api_key.strip()}) if api_key and api_key.strip() else None
    )
    row = (
        (
            await session.execute(
                text(
                    f"""
                    INSERT INTO provider_profiles
                        (id, name, provider, base_url, default_model, api_key_ciphertext,
                         context_window_tokens, enabled, metadata)
                    VALUES
                        (:id, :name, :provider, :base_url, :default_model, :ciphertext,
                         :context_window_tokens, :enabled, CAST(:metadata AS jsonb))
                    RETURNING {_COLUMNS}
                    """
                ),
                {
                    "id": profile_id,
                    "name": name.strip(),
                    "provider": provider,
                    "base_url": base_url.rstrip("/"),
                    "default_model": default_model.strip(),
                    "ciphertext": ciphertext,
                    "context_window_tokens": context_window_tokens,
                    "enabled": enabled,
                    "metadata": _json(metadata),
                },
            )
        )
        .mappings()
        .one()
    )
    return _record(row)


async def update_provider_profile(
    session: AsyncSession,
    *,
    profile_id: UUID,
    changes: dict[str, Any],
    secret_store: LocalSecretStore,
) -> ProviderProfileRecord | None:
    current = await get_provider_profile(session, profile_id)
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
    for key in ("name", "base_url", "default_model", "context_window_tokens", "enabled", "metadata"):
        if key in changes and changes[key] is not None:
            fields[key] = changes[key]
    if changes.get("clear_api_key"):
        fields["ciphertext"] = None
    elif changes.get("api_key") is not None:
        key = str(changes["api_key"]).strip()
        fields["ciphertext"] = secret_store.encrypt({"api_key": key}) if key else None
    row = (
        (
            await session.execute(
                text(
                    f"""
                    UPDATE provider_profiles
                    SET name = :name, base_url = :base_url, default_model = :default_model,
                        api_key_ciphertext = :ciphertext,
                        context_window_tokens = :context_window_tokens,
                        enabled = :enabled, metadata = CAST(:metadata AS jsonb)
                    WHERE id = :id
                    RETURNING {_COLUMNS}
                    """
                ),
                {
                    "id": profile_id,
                    "name": str(fields["name"]).strip(),
                    "base_url": str(fields["base_url"]).rstrip("/"),
                    "default_model": str(fields["default_model"]).strip(),
                    "ciphertext": fields["ciphertext"],
                    "context_window_tokens": fields["context_window_tokens"],
                    "enabled": fields["enabled"],
                    "metadata": _json(fields["metadata"]),
                },
            )
        )
        .mappings()
        .one()
    )
    return _record(row)


async def delete_provider_profile(session: AsyncSession, profile_id: UUID) -> bool:
    result = await session.execute(
        text("DELETE FROM provider_profiles WHERE id = :id RETURNING id"), {"id": profile_id}
    )
    return result.scalar_one_or_none() is not None


async def build_conversation_gateway(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
) -> ModelGateway:
    row = (
        (
            await session.execute(
                text(
                    """
                    SELECT p.id, p.name, p.provider, p.base_url, p.default_model,
                           p.api_key_ciphertext, p.context_window_tokens, p.enabled,
                           p.metadata, p.created_at, p.updated_at, c.model_override
                    FROM conversations c
                    LEFT JOIN provider_profiles p ON p.id = c.provider_profile_id
                    WHERE c.id = :conversation_id
                    """
                ),
                {"conversation_id": conversation_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    audit = SqlLlmCallAudit(session)
    budget = build_cost_guard(settings, session_factory)
    if row is None or row["id"] is None:
        return build_model_gateway(settings, audit_sink=audit, budget_guard=budget, run_id=run_id)
    profile = _record({key: row[key] for key in _COLUMN_NAMES})
    if not profile.enabled:
        raise RuntimeError(f"会话选择的 Provider {profile.name} 已停用，请重新选择")
    secrets = LocalSecretStore(settings.secret_store_key_path).decrypt(profile.api_key_ciphertext)
    api_key = str(secrets.get("api_key") or "")
    if profile.provider != "ollama" and not api_key:
        raise RuntimeError(f"Provider {profile.name} 缺少 API Key，请在模型设置中补充")
    model = str(row["model_override"] or profile.default_model).strip()
    chat_provider = build_chat_provider(
        ChatProviderConfig(
            provider=profile.provider,
            base_url=profile.base_url,
            api_key=api_key,
            model=model,
            timeout_s=settings.model_timeout_s,
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


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
