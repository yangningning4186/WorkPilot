"""Cowork 交付物索引；文件内容仍留在用户授权目录。"""

from pathlib import Path
from typing import Any
from uuid import UUID

from app.core.db import DbSession as AsyncSession
from app.cowork_contracts import (
    ArtifactKind as ArtifactKind,
)
from app.cowork_contracts import (
    ArtifactRecord as ArtifactRecord,
)
from app.cowork_contracts import (
    ArtifactRegistrationError as ArtifactRegistrationError,
)
from app.cowork_store.routing import cowork_store

_COLUMNS = """
    id, conversation_id, run_id, session_root_id, kind, title, uri, mime_type, meta,
    created_at, updated_at
"""
_QUALIFIED_ARTIFACT_COLUMNS = ", ".join(
    f"artifacts.{column.strip()} AS {column.strip()}" for column in _COLUMNS.split(",")
)


async def list_artifacts(session: AsyncSession, *, conversation_id: UUID) -> list[ArtifactRecord]:
    store = cowork_store()
    return await store.list_artifacts(conversation_id=conversation_id)


async def resolve_artifact_file(
    session: AsyncSession, *, artifact_id: UUID
) -> tuple[ArtifactRecord, Path] | None:
    store = cowork_store()
    return await store.resolve_artifact_file(artifact_id=artifact_id)


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
    store = cowork_store()
    return await store.register_artifact(
        conversation_id=conversation_id,
        kind=kind,
        title=title,
        uri=uri,
        run_id=run_id,
        session_root_id=session_root_id,
        mime_type=mime_type,
        meta=meta,
    )
