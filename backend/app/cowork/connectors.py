"""连接器账户与加密凭据：`<cowork_data_path>/connector_accounts.json`（0600）。

和 Provider profile 同一套形状（见 `provider_profiles.py` 的模块说明），凭据同样只
以密文落盘，主密钥在 `LocalSecretStore` 手里。openworker 把 token 按
`<connector>:account:<id>` 分区放进一个 `secrets.json`；这里保留 UUID 主键，因为
Inbox 绑定和消息投递都按 account_id 引用，换成"连接器名 + 账号名"会让重命名变成
一次跨文件的改键。
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
from app.core.private_json import read_private_json, write_private_json
from app.cowork.connector_descriptors import connector_kinds
from app.schemas.connectors import ConnectorAuthType, ConnectorKind, ConnectorStatus
from app.security.secret_store import LocalSecretStore

_STORE_FILE = "connector_accounts.json"
_KINDS = connector_kinds()
_AUTH_TYPES: frozenset[str] = frozenset({"oauth2", "token", "app_credentials"})
_STATUSES: frozenset[str] = frozenset(
    {"configured", "authorizing", "connected", "expired", "error"}
)
_PUBLIC_FIELDS = (
    "id",
    "kind",
    "name",
    "auth_type",
    "status",
    "config",
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
_lock = threading.Lock()


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
            **{key: getattr(self, key) for key in _PUBLIC_FIELDS},
            "has_secrets": bool(self.secret_ciphertext),
        }


class ConnectorNameTakenError(ValueError):
    """同一 kind 下名称唯一——原来那条 `(kind, lower(name))` 唯一索引没有了。"""


def store_path(settings: Settings) -> Path:
    return settings.cowork_data_path.expanduser() / _STORE_FILE


def _now() -> datetime:
    return datetime.now(UTC)


def _time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _read_all(path: Path) -> dict[str, dict[str, Any]]:
    raw = read_private_json(path).get("accounts")
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if isinstance(value, dict)}


def _write_all(path: Path, accounts: dict[str, dict[str, Any]]) -> None:
    write_private_json(path, {"version": 1, "accounts": accounts})


def _record(account_id: str, value: dict[str, Any]) -> ConnectorAccountRecord | None:
    """坏条目跳过而不是抛——理由同 provider_profiles._record。"""

    try:
        kind = str(value["kind"])
        auth_type = str(value["auth_type"])
        status = str(value.get("status") or "configured")
        if kind not in _KINDS or auth_type not in _AUTH_TYPES or status not in _STATUSES:
            return None
        ciphertext = value.get("secret_ciphertext")
        scopes = value.get("scopes")
        return ConnectorAccountRecord(
            id=UUID(account_id),
            kind=kind,
            name=str(value["name"]),
            auth_type=auth_type,  # type: ignore[arg-type]
            status=status,  # type: ignore[arg-type]
            config=dict(value.get("config") or {}),
            secret_ciphertext=str(ciphertext) if ciphertext else None,
            scopes=[str(item) for item in scopes] if isinstance(scopes, list) else [],
            external_account_id=_optional_text(value.get("external_account_id")),
            external_account_name=_optional_text(value.get("external_account_name")),
            expires_at=_time(value.get("expires_at")),
            last_checked_at=_time(value.get("last_checked_at")),
            last_error=_optional_text(value.get("last_error")),
            enabled=bool(value.get("enabled", True)),
            created_at=_time(value.get("created_at")) or _now(),
            updated_at=_time(value.get("updated_at")) or _now(),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _optional_text(value: object) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def _serialize(record: ConnectorAccountRecord) -> dict[str, Any]:
    return {
        "kind": record.kind,
        "name": record.name,
        "auth_type": record.auth_type,
        "status": record.status,
        "config": record.config,
        "secret_ciphertext": record.secret_ciphertext,
        "scopes": record.scopes,
        "external_account_id": record.external_account_id,
        "external_account_name": record.external_account_name,
        "expires_at": None if record.expires_at is None else record.expires_at.isoformat(),
        "last_checked_at": (
            None if record.last_checked_at is None else record.last_checked_at.isoformat()
        ),
        "last_error": record.last_error,
        "enabled": record.enabled,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }


def list_connector_accounts(settings: Settings) -> list[ConnectorAccountRecord]:
    records = [
        record
        for account_id, value in _read_all(store_path(settings)).items()
        if (record := _record(account_id, value)) is not None
    ]
    records.sort(key=lambda item: (item.name, item.id.hex))
    return records


def get_connector_account(settings: Settings, account_id: UUID) -> ConnectorAccountRecord | None:
    value = _read_all(store_path(settings)).get(str(account_id))
    return None if value is None else _record(str(account_id), value)


def create_connector_account(
    settings: Settings,
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
    now = _now()
    record = ConnectorAccountRecord(
        id=uuid7(),
        kind=kind,
        name=name,
        auth_type=auth_type,
        status="connected" if access_token else "configured",
        config=public_config,
        secret_ciphertext=secret_store.encrypt(secrets) if secrets else None,
        scopes=scopes,
        external_account_id=None,
        external_account_name=None,
        expires_at=None,
        last_checked_at=None,
        last_error=None,
        enabled=enabled,
        created_at=now,
        updated_at=now,
    )
    path = store_path(settings)
    with _lock:
        accounts = _read_all(path)
        _reject_duplicate_name(accounts, kind=kind, name=name, keep=None)
        accounts[str(record.id)] = _serialize(record)
        _write_all(path, accounts)
    return record


def update_connector_account(
    settings: Settings,
    *,
    account_id: UUID,
    changes: dict[str, Any],
    secret_store: LocalSecretStore,
) -> ConnectorAccountRecord | None:
    path = store_path(settings)
    with _lock:
        accounts = _read_all(path)
        value = accounts.get(str(account_id))
        current = None if value is None else _record(str(account_id), value)
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
                secret_value = str(changes[key]).strip()
                if secret_value:
                    secrets[key] = secret_value
                else:
                    secrets.pop(key, None)
        status_value: ConnectorStatus = current.status
        if changes.get("clear_secrets"):
            status_value = "configured"
        elif changes.get("access_token"):
            status_value = "connected"
        name = str(changes.get("name") or current.name).strip()
        _reject_duplicate_name(accounts, kind=current.kind, name=name, keep=str(account_id))
        updated = ConnectorAccountRecord(
            id=current.id,
            kind=current.kind,
            name=name,
            auth_type=current.auth_type,
            status=status_value,
            config=config,
            secret_ciphertext=secret_store.encrypt(secrets) if secrets else None,
            scopes=[str(item) for item in (changes.get("scopes") or current.scopes)],
            external_account_id=current.external_account_id,
            external_account_name=current.external_account_name,
            expires_at=current.expires_at,
            last_checked_at=current.last_checked_at,
            last_error=None,
            enabled=(
                current.enabled if changes.get("enabled") is None else bool(changes["enabled"])
            ),
            created_at=current.created_at,
            updated_at=_now(),
        )
        accounts[str(account_id)] = _serialize(updated)
        _write_all(path, accounts)
    return updated


def delete_connector_account(settings: Settings, account_id: UUID) -> bool:
    path = store_path(settings)
    with _lock:
        accounts = _read_all(path)
        if accounts.pop(str(account_id), None) is None:
            return False
        _write_all(path, accounts)
    return True


def set_connector_status(
    settings: Settings,
    *,
    account_id: UUID,
    status: ConnectorStatus,
    secret_ciphertext: str | None = None,
    external_account_id: str | None = None,
    external_account_name: str | None = None,
    expires_at: datetime | None = None,
    error: str | None = None,
) -> None:
    """None 表示"保持原值"，对应原来那几个 `COALESCE(:x, x)`。

    `expires_at` 是例外：它跟着新 token 走，None 就是"这次换来的 token 不过期"。
    """

    path = store_path(settings)
    with _lock:
        accounts = _read_all(path)
        value = accounts.get(str(account_id))
        current = None if value is None else _record(str(account_id), value)
        if current is None:
            return
        updated = ConnectorAccountRecord(
            id=current.id,
            kind=current.kind,
            name=current.name,
            auth_type=current.auth_type,
            status=status,
            config=current.config,
            secret_ciphertext=secret_ciphertext or current.secret_ciphertext,
            scopes=current.scopes,
            external_account_id=external_account_id or current.external_account_id,
            external_account_name=external_account_name or current.external_account_name,
            expires_at=expires_at,
            last_checked_at=_now(),
            last_error=error,
            enabled=current.enabled,
            created_at=current.created_at,
            updated_at=_now(),
        )
        accounts[str(account_id)] = _serialize(updated)
        _write_all(path, accounts)


def connector_secrets(
    account: ConnectorAccountRecord, secret_store: LocalSecretStore
) -> dict[str, Any]:
    return secret_store.decrypt(account.secret_ciphertext)


def _reject_duplicate_name(
    accounts: dict[str, dict[str, Any]], *, kind: str, name: str, keep: str | None
) -> None:
    folded = name.strip().casefold()
    for account_id, value in accounts.items():
        if account_id == keep or str(value.get("kind")) != kind:
            continue
        if str(value.get("name", "")).strip().casefold() == folded:
            raise ConnectorNameTakenError(f"{kind} 下已存在同名账户: {name}")


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
