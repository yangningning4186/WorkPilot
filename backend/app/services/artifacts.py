"""Cowork 交付物索引；文件内容仍留在用户授权目录。"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.services.cowork_permissions import (
    CapabilityDeniedError,
    ConversationNotFoundError,
    resolve_target_within_root,
)

ArtifactKind = Literal["file", "report", "diff", "table"]


class ArtifactRegistrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ArtifactRecord:
    id: UUID
    conversation_id: UUID
    run_id: UUID | None
    session_root_id: UUID | None
    kind: ArtifactKind
    title: str
    uri: str
    mime_type: str | None
    meta: dict[str, Any]
    created_at: datetime
    updated_at: datetime


_COLUMNS = """
    id, conversation_id, run_id, session_root_id, kind, title, uri, mime_type, meta,
    created_at, updated_at
"""


async def list_artifacts(
    session: AsyncSession, *, conversation_id: UUID
) -> list[ArtifactRecord]:
    owner = (
        await session.execute(
            text(
                """
                SELECT id FROM conversations
                WHERE id = :conversation_id AND scope = 'local_owner'
                  AND demo_session_id IS NULL
                """
            ),
            {"conversation_id": conversation_id},
        )
    ).scalar_one_or_none()
    if owner is None:
        raise ConversationNotFoundError(str(conversation_id))
    rows = (
        (
            await session.execute(
                text(
                    f"""
                    SELECT {_COLUMNS} FROM artifacts
                    WHERE conversation_id = :conversation_id
                    ORDER BY created_at DESC, id DESC
                    """
                ),
                {"conversation_id": conversation_id},
            )
        )
        .mappings()
        .all()
    )
    return [ArtifactRecord(**row) for row in rows]


async def register_artifact(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    kind: ArtifactKind,
    title: str,
    uri: str,
    run_id: UUID | None = None,
    session_root_id: UUID | None = None,
    mime_type: str | None = None,
    meta: dict[str, Any] | None = None,
) -> ArtifactRecord:
    if kind not in {"file", "report", "diff", "table"}:
        raise ValueError("未知 artifact kind")
    if not title.strip() or not uri.strip():
        raise ValueError("artifact 标题与 URI 不能为空")
    owner = (
        await session.execute(
            text(
                """
                SELECT id FROM conversations
                WHERE id = :conversation_id AND scope = 'local_owner'
                  AND demo_session_id IS NULL
                """
            ),
            {"conversation_id": conversation_id},
        )
    ).scalar_one_or_none()
    if owner is None:
        raise ConversationNotFoundError(str(conversation_id))
    if run_id is not None:
        run_exists = (
            await session.execute(
                text(
                    """
                    SELECT id FROM agent_runs
                    WHERE id = :run_id AND conversation_id = :conversation_id
                    """
                ),
                {"run_id": run_id, "conversation_id": conversation_id},
            )
        ).scalar_one_or_none()
        if run_exists is None:
            raise ArtifactRegistrationError("artifact 的 run 不属于当前 Cowork 会话")

    stored_uri = uri.strip()
    if session_root_id is not None:
        canonical_root = (
            await session.execute(
                text(
                    """
                    SELECT canonical_path FROM session_roots
                    WHERE id = :root_id AND conversation_id = :conversation_id
                      AND enabled = true
                    """
                ),
                {"root_id": session_root_id, "conversation_id": conversation_id},
            )
        ).scalar_one_or_none()
        if canonical_root is None:
            raise ArtifactRegistrationError("artifact 绑定的会话目录不存在或已撤销")
        try:
            stored_uri = str(
                resolve_target_within_root(Path(canonical_root), Path(stored_uri))
            )
        except CapabilityDeniedError as error:
            raise ArtifactRegistrationError("artifact 路径不在绑定的会话目录内") from error
    row = (
        (
            await session.execute(
                text(
                    f"""
                    INSERT INTO artifacts
                        (id, conversation_id, run_id, session_root_id, kind, title, uri,
                         mime_type, meta)
                    VALUES
                        (:id, :conversation_id, :run_id, :session_root_id, :kind, :title,
                         :uri, :mime_type, CAST(:meta AS jsonb))
                    RETURNING {_COLUMNS}
                    """
                ),
                {
                    "id": uuid7(),
                    "conversation_id": conversation_id,
                    "run_id": run_id,
                    "session_root_id": session_root_id,
                    "kind": kind,
                    "title": title.strip(),
                    "uri": stored_uri,
                    "mime_type": mime_type,
                    "meta": json.dumps(meta or {}),
                },
            )
        )
        .mappings()
        .one()
    )
    return ArtifactRecord(**row)
