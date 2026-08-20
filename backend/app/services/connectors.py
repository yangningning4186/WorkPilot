"""连接器账户、OAuth state 与加密凭据持久化。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.schemas.connectors import ConnectorAuthType, ConnectorKind, ConnectorStatus
from app.security.secret_store import LocalSecretStore

_COLUMN_NAMES = (
    "id",
    "kind",
    "name",
    "auth_type",
    "status",
    "config",
    "secret_ciphertext",
    "scopes",
    "external_account_id",
    "external_account_name",
    "expires_at",
    "last_checked_at",
    "last_error",
    "enabled",
    "created_at",
    "updated_at",
)
_COLUMNS = ", ".join(_COLUMN_NAMES)


@dataclass(frozen=True)
class ConnectorAccountRecord:
    id: UUID
    kind: ConnectorKind
    name: str
    auth_type: ConnectorAuthType
    status: ConnectorStatus
    config: dict[str, Any]
    secret_ciphertext: str | None
    scopes: list[str]
    external_account_id: str | None
    external_account_name: str | None
    expires_at: datetime | None
    last_checked_at: datetime | None
    last_error: str | None
    enabled: bool
    created_at: datetime
    updated_at: datetime

    def public(self) -> dict[str, Any]:
        return {
            **{key: getattr(self, key) for key in _COLUMN_NAMES if key != "secret_ciphertext"},
            "has_secrets": bool(self.secret_ciphertext),
        }


def _record(row: Any) -> ConnectorAccountRecord:
    value = dict(row)
    value["config"] = dict(value.get("config") or {})
    value["scopes"] = [str(item) for item in value.get("scopes") or []]
    return ConnectorAccountRecord(**value)


async def list_connector_accounts(session: AsyncSession) -> list[ConnectorAccountRecord]:
    rows = (
        (
            await session.execute(
                text(f"SELECT {_COLUMNS} FROM connector_accounts ORDER BY name, id")
            )
        )
        .mappings()
        .all()
    )
    return [_record(row) for row in rows]


async def get_connector_account(
    session: AsyncSession, account_id: UUID
) -> ConnectorAccountRecord | None:
    row = (
        (
            await session.execute(
                text(f"SELECT {_COLUMNS} FROM connector_accounts WHERE id = :id"),
                {"id": account_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _record(row)


async def create_connector_account(
    session: AsyncSession,
    *,
    kind: ConnectorKind,
    name: str,
    auth_type: ConnectorAuthType,
    client_id: str | None,
    client_secret: str | None,
    access_token: str | None,
    refresh_token: str | None,
    redirect_uri: str | None,
    scopes: list[str],
    config: dict[str, Any],
    enabled: bool,
    secret_store: LocalSecretStore,
) -> ConnectorAccountRecord:
    public_config = {**config, "client_id": (client_id or "").strip()}
    if redirect_uri:
        public_config["redirect_uri"] = redirect_uri
    secrets = _secret_payload(client_secret, access_token, refresh_token)
    status: ConnectorStatus = "connected" if access_token else "configured"
    row = (
        (
            await session.execute(
                text(
                    f"""
                    INSERT INTO connector_accounts
                        (id, kind, name, auth_type, status, config, secret_ciphertext,
                         scopes, enabled)
                    VALUES
                        (:id, :kind, :name, :auth_type, :status, CAST(:config AS jsonb),
                         :ciphertext, CAST(:scopes AS jsonb), :enabled)
                    RETURNING {_COLUMNS}
                    """
                ),
                {
                    "id": uuid7(),
                    "kind": kind,
                    "name": name,
                    "auth_type": auth_type,
                    "status": status,
                    "config": _json(public_config),
                    "ciphertext": secret_store.encrypt(secrets) if secrets else None,
                    "scopes": _json(scopes),
                    "enabled": enabled,
                },
            )
        )
        .mappings()
        .one()
    )
    return _record(row)


async def update_connector_account(
    session: AsyncSession,
    *,
    account_id: UUID,
    changes: dict[str, Any],
    secret_store: LocalSecretStore,
) -> ConnectorAccountRecord | None:
    current = await get_connector_account(session, account_id)
    if current is None:
        return None
    config = dict(current.config)
    if changes.get("config") is not None:
        config.update(dict(changes["config"]))
    for key in ("client_id", "redirect_uri"):
        if changes.get(key) is not None:
            config[key] = str(changes[key]).strip()
    secrets = (
        {} if changes.get("clear_secrets") else secret_store.decrypt(current.secret_ciphertext)
    )
    for key in ("client_secret", "access_token", "refresh_token"):
        if changes.get(key) is not None:
            value = str(changes[key]).strip()
            if value:
                secrets[key] = value
            else:
                secrets.pop(key, None)
    status_value: ConnectorStatus = current.status
    if changes.get("clear_secrets"):
        status_value = "configured"
    elif changes.get("access_token"):
        status_value = "connected"
    row = (
        (
            await session.execute(
                text(
                    f"""
                    UPDATE connector_accounts
                    SET name = :name, status = :status, config = CAST(:config AS jsonb),
                        secret_ciphertext = :ciphertext, scopes = CAST(:scopes AS jsonb),
                        enabled = :enabled, last_error = NULL
                    WHERE id = :id
                    RETURNING {_COLUMNS}
                    """
                ),
                {
                    "id": account_id,
                    "name": str(changes.get("name") or current.name).strip(),
                    "status": status_value,
                    "config": _json(config),
                    "ciphertext": secret_store.encrypt(secrets) if secrets else None,
                    "scopes": _json(changes.get("scopes") or current.scopes),
                    "enabled": current.enabled
                    if changes.get("enabled") is None
                    else bool(changes["enabled"]),
                },
            )
        )
        .mappings()
        .one()
    )
    return _record(row)


async def delete_connector_account(session: AsyncSession, account_id: UUID) -> bool:
    result = await session.execute(
        text("DELETE FROM connector_accounts WHERE id = :id RETURNING id"), {"id": account_id}
    )
    return result.scalar_one_or_none() is not None


async def set_connector_status(
    session: AsyncSession,
    *,
    account_id: UUID,
    status: ConnectorStatus,
    secret_ciphertext: str | None = None,
    external_account_id: str | None = None,
    external_account_name: str | None = None,
    expires_at: datetime | None = None,
    error: str | None = None,
) -> None:
    await session.execute(
        text(
            """
            UPDATE connector_accounts
            SET status = :status,
                secret_ciphertext = COALESCE(:secret_ciphertext, secret_ciphertext),
                external_account_id = COALESCE(:external_account_id, external_account_id),
                external_account_name = COALESCE(:external_account_name, external_account_name),
                expires_at = :expires_at, last_checked_at = now(), last_error = :error
            WHERE id = :id
            """
        ),
        {
            "id": account_id,
            "status": status,
            "secret_ciphertext": secret_ciphertext,
            "external_account_id": external_account_id,
            "external_account_name": external_account_name,
            "expires_at": expires_at,
            "error": error,
        },
    )


def connector_secrets(
    account: ConnectorAccountRecord, secret_store: LocalSecretStore
) -> dict[str, Any]:
    return secret_store.decrypt(account.secret_ciphertext)


def _secret_payload(
    client_secret: str | None, access_token: str | None, refresh_token: str | None
) -> dict[str, str]:
    return {
        key: value.strip()
        for key, value in {
            "client_secret": client_secret or "",
            "access_token": access_token or "",
            "refresh_token": refresh_token or "",
        }.items()
        if value.strip()
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
