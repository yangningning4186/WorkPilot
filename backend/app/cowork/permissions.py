"""Cowork 会话目录与 capability grant。

目录 grant 是用户主动授予的长期会话能力。工具每次执行仍需重新规范化目标路径并
检查 grant，但不会再要求逐操作确认。
"""

import asyncio
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from app.core.db import DbSession as AsyncSession
from app.cowork_contracts import (
    AccessMode as AccessMode,
)
from app.cowork_contracts import (
    ActiveCapability as ActiveCapability,
)
from app.cowork_contracts import (
    Capability as Capability,
)
from app.cowork_contracts import (
    CapabilityDeniedError as CapabilityDeniedError,
)
from app.cowork_contracts import (
    CapabilityGrantRecord as CapabilityGrantRecord,
)
from app.cowork_contracts import (
    ConversationNotFoundError as ConversationNotFoundError,
)
from app.cowork_contracts import (
    CoworkPermissionError as CoworkPermissionError,
)
from app.cowork_contracts import (
    PathAuthorization as PathAuthorization,
)
from app.cowork_contracts import (
    SessionRootNotFoundError as SessionRootNotFoundError,
)
from app.cowork_contracts import (
    SessionRootRecord as SessionRootRecord,
)
from app.cowork_policy import (
    ACTIVE_CAPABILITIES as ACTIVE_CAPABILITIES,
)
from app.cowork_policy import (
    ALL_CAPABILITIES as ALL_CAPABILITIES,
)
from app.cowork_policy import (
    GLOBAL_CAPABILITIES as GLOBAL_CAPABILITIES,
)
from app.cowork_policy import (
    LEGACY_CAPABILITIES as LEGACY_CAPABILITIES,
)
from app.cowork_policy import (
    PATH_CAPABILITIES as PATH_CAPABILITIES,
)
from app.cowork_policy import (
    canonicalize_root as canonicalize_root,
)
from app.cowork_policy import (
    resolve_target_within_root as resolve_target_within_root,
)
from app.cowork_store.routing import cowork_store

_WORD_SUFFIXES = frozenset({".docx"})
_EXCEL_SUFFIXES = frozenset({".xlsx"})
DEFAULT_WORKSPACE_LABEL = "WorkPilot 默认文件夹"


_ROOT_COLUMNS = """
    id, conversation_id, requested_path, canonical_path, label, access_mode, enabled,
    created_at, updated_at
"""
_GRANT_COLUMNS = """
    id, conversation_id, session_root_id, capability, grant_source, expires_at,
    revoked_at, created_at, updated_at
"""


def _prepare_managed_workspace(path: Path) -> str:
    """创建仅当前用户可访问的 WorkPilot 默认输出目录。"""

    resolved = path.expanduser().resolve(strict=False)
    process_cwd = Path.cwd().resolve()
    if process_cwd == resolved or process_cwd.is_relative_to(resolved):
        raise CoworkPermissionError("WorkPilot 默认文件夹不能是项目或应用工作目录")
    try:
        resolved.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved.chmod(0o700)
    except OSError as error:
        raise CoworkPermissionError("无法创建 WorkPilot 默认文件夹") from error
    if not resolved.is_dir():
        raise CoworkPermissionError("WorkPilot 默认文件夹路径不是目录")
    return str(resolved)


async def ensure_default_session_root(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    workspace_path: Path,
) -> SessionRootRecord:
    """为普通任务和自动化幂等挂载 WorkPilot 默认读写目录。"""

    prepared = await asyncio.to_thread(_prepare_managed_workspace, workspace_path)
    canonical_default = Path(prepared)
    roots = await list_session_roots(session, conversation_id=conversation_id)
    for root in roots:
        if root.label == DEFAULT_WORKSPACE_LABEL and Path(root.canonical_path) != canonical_default:
            await revoke_session_root(
                session,
                conversation_id=conversation_id,
                root_id=root.id,
            )
    return await create_session_root(
        session,
        conversation_id=conversation_id,
        requested_path=prepared,
        access_mode="read_write",
        label=DEFAULT_WORKSPACE_LABEL,
    )


async def _ensure_owner_conversation(session: AsyncSession, conversation_id: UUID) -> None:
    del session  # 会话表已经在本机 store 里
    if not await cowork_store().conversation_exists(conversation_id):
        raise ConversationNotFoundError(str(conversation_id))


def _root_record(row: Any) -> SessionRootRecord:
    return SessionRootRecord(**row)


def _grant_record(row: Any) -> CapabilityGrantRecord:
    return CapabilityGrantRecord(**row)


async def create_session_root(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    requested_path: str,
    access_mode: AccessMode,
    label: str | None = None,
) -> SessionRootRecord:
    """登记用户选取的目录，并一次性授予对应文件能力。"""

    store = cowork_store()
    return await store.create_session_root(
        conversation_id=conversation_id,
        requested_path=requested_path,
        access_mode=access_mode,
        label=label,
    )


async def list_session_roots(
    session: AsyncSession, *, conversation_id: UUID
) -> list[SessionRootRecord]:
    store = cowork_store()
    return await store.list_session_roots(conversation_id=conversation_id)


async def revoke_session_root(
    session: AsyncSession, *, conversation_id: UUID, root_id: UUID
) -> bool:
    store = cowork_store()
    return await store.revoke_session_root(conversation_id=conversation_id, root_id=root_id)


async def grant_capability(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    capability: Capability,
    session_root_id: UUID | None = None,
    resource_scope: str | None = None,
    grant_source: Literal["user", "policy"] = "user",
    expires_in_s: int | None = None,
) -> CapabilityGrantRecord:
    store = cowork_store()
    return await store.grant_capability(
        conversation_id=conversation_id,
        capability=capability,
        session_root_id=session_root_id,
        resource_scope=resource_scope,
        grant_source=grant_source,
        expires_in_s=expires_in_s,
    )


async def list_capability_grants(
    session: AsyncSession, *, conversation_id: UUID
) -> list[CapabilityGrantRecord]:
    store = cowork_store()
    return await store.list_capability_grants(conversation_id=conversation_id)


async def revoke_capability_grant(
    session: AsyncSession, *, conversation_id: UUID, grant_id: UUID
) -> bool:
    store = cowork_store()
    return await store.revoke_capability_grant(conversation_id=conversation_id, grant_id=grant_id)


async def authorize_capability(
    session: AsyncSession, *, conversation_id: UUID, capability: Capability
) -> CapabilityGrantRecord:
    store = cowork_store()
    return await store.authorize_capability(conversation_id=conversation_id, capability=capability)


async def authorize_scoped_capability(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    capability: Capability,
    target: str,
) -> CapabilityGrantRecord:
    store = cowork_store()
    return await store.authorize_scoped_capability(
        conversation_id=conversation_id,
        capability=capability,
        target=target,
    )


async def authorize_path(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    target_path: Path,
    capability: Capability,
) -> PathAuthorization:
    store = cowork_store()
    return await store.authorize_path(
        conversation_id=conversation_id,
        target_path=target_path,
        capability=capability,
    )
