"""Cowork 会话目录与 capability grant。

目录 grant 是用户主动授予的长期会话能力。工具每次执行仍需重新规范化目标路径并
检查 grant，但不会再要求逐操作确认。
"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.cowork_contracts import (
    AccessMode as AccessMode,
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
    ALL_CAPABILITIES as ALL_CAPABILITIES,
)
from app.cowork_policy import (
    GLOBAL_CAPABILITIES as GLOBAL_CAPABILITIES,
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
from app.cowork_store.routing import configured_cowork_store

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
        if (
            root.label == DEFAULT_WORKSPACE_LABEL
            and Path(root.canonical_path) != canonical_default
        ):
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
    found = (
        await session.execute(
            text(
                """
                SELECT id FROM conversations
                WHERE id = :conversation_id
                  AND scope = 'local_owner'
                  AND demo_session_id IS NULL
                """
            ),
            {"conversation_id": conversation_id},
        )
    ).scalar_one_or_none()
    if found is None:
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

    store = configured_cowork_store()
    if store is not None:
        return await store.create_session_root(
            conversation_id=conversation_id,
            requested_path=requested_path,
            access_mode=access_mode,
            label=label,
        )

    if access_mode not in {"read_only", "read_write"}:
        raise ValueError("access_mode 只能是 read_only 或 read_write")
    await _ensure_owner_conversation(session, conversation_id)
    canonical = canonicalize_root(requested_path)
    display_label = (label or canonical.name or str(canonical)).strip()
    if not display_label:
        raise ValueError("目录标签不能为空")

    root_id = uuid7()
    row = (
        (
            await session.execute(
                text(
                    f"""
                    INSERT INTO session_roots
                        (id, conversation_id, requested_path, canonical_path, label, access_mode)
                    VALUES
                        (:id, :conversation_id, :requested_path, :canonical_path, :label,
                         :access_mode)
                    ON CONFLICT (conversation_id, canonical_path) DO UPDATE SET
                        requested_path = EXCLUDED.requested_path,
                        label = EXCLUDED.label,
                        access_mode = EXCLUDED.access_mode,
                        enabled = true
                    RETURNING {_ROOT_COLUMNS}
                    """
                ),
                {
                    "id": root_id,
                    "conversation_id": conversation_id,
                    "requested_path": requested_path,
                    "canonical_path": str(canonical),
                    "label": display_label,
                    "access_mode": access_mode,
                },
            )
        )
        .mappings()
        .one()
    )
    root = _root_record(row)

    capabilities: tuple[Capability, ...] = ("filesystem.read",)
    if access_mode == "read_write":
        capabilities += (
            "filesystem.write",
            "office.word.edit",
            "office.excel.edit",
        )
    else:
        await session.execute(
            text(
                """
                UPDATE capability_grants
                SET revoked_at = now()
                WHERE conversation_id = :conversation_id
                  AND session_root_id = :root_id
                  AND capability IN (
                    'filesystem.write', 'office.word.edit', 'office.excel.edit'
                  )
                  AND revoked_at IS NULL
                """
            ),
            {"conversation_id": conversation_id, "root_id": root.id},
        )
    for capability in capabilities:
        await grant_capability(
            session,
            conversation_id=conversation_id,
            capability=capability,
            session_root_id=root.id,
            grant_source="user",
        )
    return root


async def list_session_roots(
    session: AsyncSession, *, conversation_id: UUID
) -> list[SessionRootRecord]:
    store = configured_cowork_store()
    if store is not None:
        return await store.list_session_roots(conversation_id=conversation_id)
    await _ensure_owner_conversation(session, conversation_id)
    rows = (
        (
            await session.execute(
                text(
                    f"""
                    SELECT {_ROOT_COLUMNS}
                    FROM session_roots
                    WHERE conversation_id = :conversation_id AND enabled = true
                    ORDER BY CASE WHEN label = :default_label THEN 1 ELSE 0 END,
                             created_at, id
                    """
                ),
                {
                    "conversation_id": conversation_id,
                    "default_label": DEFAULT_WORKSPACE_LABEL,
                },
            )
        )
        .mappings()
        .all()
    )
    return [_root_record(row) for row in rows]


async def revoke_session_root(
    session: AsyncSession, *, conversation_id: UUID, root_id: UUID
) -> bool:
    store = configured_cowork_store()
    if store is not None:
        return await store.revoke_session_root(conversation_id=conversation_id, root_id=root_id)
    await _ensure_owner_conversation(session, conversation_id)
    changed = await session.execute(
        text(
            """
            UPDATE session_roots
            SET enabled = false
            WHERE id = :root_id AND conversation_id = :conversation_id AND enabled = true
            RETURNING id
            """
        ),
        {"root_id": root_id, "conversation_id": conversation_id},
    )
    if changed.scalar_one_or_none() is None:
        return False
    await session.execute(
        text(
            """
            UPDATE capability_grants
            SET revoked_at = now()
            WHERE conversation_id = :conversation_id
              AND session_root_id = :root_id
              AND revoked_at IS NULL
            """
        ),
        {"conversation_id": conversation_id, "root_id": root_id},
    )
    return True


async def grant_capability(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    capability: Capability,
    session_root_id: UUID | None = None,
    grant_source: Literal["user", "policy"] = "user",
    expires_in_s: int | None = None,
) -> CapabilityGrantRecord:
    store = configured_cowork_store()
    if store is not None:
        return await store.grant_capability(
            conversation_id=conversation_id,
            capability=capability,
            session_root_id=session_root_id,
            grant_source=grant_source,
            expires_in_s=expires_in_s,
        )
    if capability not in ALL_CAPABILITIES:
        raise ValueError("未知 capability")
    await _ensure_owner_conversation(session, conversation_id)
    if capability in PATH_CAPABILITIES and session_root_id is None:
        raise ValueError("文件 capability 必须绑定会话目录")
    if capability in GLOBAL_CAPABILITIES and session_root_id is not None:
        raise ValueError("网络/Shell/外部操作 capability 不能继承目录授权")

    if session_root_id is not None:
        root = (
            await session.execute(
                text(
                    """
                    SELECT access_mode FROM session_roots
                    WHERE id = :root_id AND conversation_id = :conversation_id
                      AND enabled = true
                    """
                ),
                {"root_id": session_root_id, "conversation_id": conversation_id},
            )
        ).scalar_one_or_none()
        if root is None:
            raise SessionRootNotFoundError(str(session_root_id))
        if capability != "filesystem.read" and root != "read_write":
            raise CapabilityDeniedError("只读目录不能授予写入或 Office 编辑能力")

    expires_at = (
        None if expires_in_s is None else datetime.now(UTC) + timedelta(seconds=expires_in_s)
    )
    # 先撤销已过期的同类授权，让部分唯一索引可接受新记录。
    await session.execute(
        text(
            """
            UPDATE capability_grants
            SET revoked_at = now()
            WHERE conversation_id = :conversation_id
              AND session_root_id IS NOT DISTINCT FROM :root_id
              AND capability = :capability
              AND revoked_at IS NULL
              AND expires_at IS NOT NULL
              AND expires_at <= now()
            """
        ),
        {
            "conversation_id": conversation_id,
            "root_id": session_root_id,
            "capability": capability,
        },
    )
    existing = (
        (
            await session.execute(
                text(
                    f"""
                    UPDATE capability_grants
                    SET expires_at = :expires_at, grant_source = :grant_source
                    WHERE conversation_id = :conversation_id
                      AND session_root_id IS NOT DISTINCT FROM :root_id
                      AND capability = :capability
                      AND revoked_at IS NULL
                    RETURNING {_GRANT_COLUMNS}
                    """
                ),
                {
                    "conversation_id": conversation_id,
                    "root_id": session_root_id,
                    "capability": capability,
                    "grant_source": grant_source,
                    "expires_at": expires_at,
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        return _grant_record(existing)

    row = (
        (
            await session.execute(
                text(
                    f"""
                    INSERT INTO capability_grants
                        (id, conversation_id, session_root_id, capability, grant_source,
                         expires_at)
                    VALUES
                        (:id, :conversation_id, :root_id, :capability, :grant_source,
                         :expires_at)
                    RETURNING {_GRANT_COLUMNS}
                    """
                ),
                {
                    "id": uuid7(),
                    "conversation_id": conversation_id,
                    "root_id": session_root_id,
                    "capability": capability,
                    "grant_source": grant_source,
                    "expires_at": expires_at,
                },
            )
        )
        .mappings()
        .one()
    )
    return _grant_record(row)


async def list_capability_grants(
    session: AsyncSession, *, conversation_id: UUID
) -> list[CapabilityGrantRecord]:
    store = configured_cowork_store()
    if store is not None:
        return await store.list_capability_grants(conversation_id=conversation_id)
    await _ensure_owner_conversation(session, conversation_id)
    rows = (
        (
            await session.execute(
                text(
                    f"""
                    SELECT {_GRANT_COLUMNS}
                    FROM capability_grants
                    WHERE conversation_id = :conversation_id
                      AND revoked_at IS NULL
                      AND (expires_at IS NULL OR expires_at > now())
                    ORDER BY created_at, id
                    """
                ),
                {"conversation_id": conversation_id},
            )
        )
        .mappings()
        .all()
    )
    return [_grant_record(row) for row in rows]


async def revoke_capability_grant(
    session: AsyncSession, *, conversation_id: UUID, grant_id: UUID
) -> bool:
    store = configured_cowork_store()
    if store is not None:
        return await store.revoke_capability_grant(
            conversation_id=conversation_id, grant_id=grant_id
        )
    await _ensure_owner_conversation(session, conversation_id)
    changed = await session.execute(
        text(
            """
            UPDATE capability_grants
            SET revoked_at = now()
            WHERE id = :grant_id AND conversation_id = :conversation_id
              AND revoked_at IS NULL
            RETURNING id
            """
        ),
        {"grant_id": grant_id, "conversation_id": conversation_id},
    )
    return changed.scalar_one_or_none() is not None


async def authorize_capability(
    session: AsyncSession, *, conversation_id: UUID, capability: Capability
) -> CapabilityGrantRecord:
    store = configured_cowork_store()
    if store is not None:
        return await store.authorize_capability(
            conversation_id=conversation_id, capability=capability
        )
    if capability not in GLOBAL_CAPABILITIES:
        raise ValueError("路径 capability 必须通过 authorize_path 校验")
    row = (
        (
            await session.execute(
                text(
                    f"""
                    SELECT {_GRANT_COLUMNS}
                    FROM capability_grants
                    WHERE conversation_id = :conversation_id
                      AND capability = :capability
                      AND session_root_id IS NULL
                      AND revoked_at IS NULL
                      AND (expires_at IS NULL OR expires_at > now())
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"conversation_id": conversation_id, "capability": capability},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise CapabilityDeniedError(f"尚未授予 {capability} 权限")
    return _grant_record(row)


async def authorize_path(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    target_path: Path,
    capability: Capability,
) -> PathAuthorization:
    store = configured_cowork_store()
    if store is not None:
        return await store.authorize_path(
            conversation_id=conversation_id,
            target_path=target_path,
            capability=capability,
        )
    if capability not in PATH_CAPABILITIES:
        raise ValueError("非路径 capability 必须通过 authorize_capability 校验")
    # Root/grant 是加法语义，不存在嵌套 deny。排序只让多个有效 grant 同时命中时
    # 优先把最具体的 root 记入审计；只读子 root 不会覆盖父级读写授权。
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT sr.id, sr.canonical_path, sr.access_mode
                    FROM session_roots sr
                    JOIN capability_grants cg
                      ON cg.session_root_id = sr.id
                     AND cg.conversation_id = sr.conversation_id
                    WHERE sr.conversation_id = :conversation_id
                      AND sr.enabled = true
                      AND cg.capability = :capability
                      AND cg.revoked_at IS NULL
                      AND (cg.expires_at IS NULL OR cg.expires_at > now())
                    ORDER BY length(sr.canonical_path) DESC
                    """
                ),
                {"conversation_id": conversation_id, "capability": capability},
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        root = Path(row["canonical_path"])
        try:
            resolved = resolve_target_within_root(root, target_path)
        except CapabilityDeniedError:
            continue
        access_mode: AccessMode = row["access_mode"]
        if capability != "filesystem.read" and access_mode != "read_write":
            continue
        suffix = resolved.suffix.lower()
        if capability == "office.word.edit" and suffix not in _WORD_SUFFIXES:
            continue
        if capability == "office.excel.edit" and suffix not in _EXCEL_SUFFIXES:
            continue
        return PathAuthorization(
            conversation_id=conversation_id,
            root_id=row["id"],
            root_path=root,
            target_path=resolved,
            access_mode=access_mode,
            capability=capability,
        )
    raise CapabilityDeniedError(f"目标路径未获得 {capability} 权限")
