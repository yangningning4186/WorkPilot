from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_owner_identity
from app.core.config import Settings, get_settings
from app.core.db import get_db_session
from app.main import create_app
from app.services.artifacts import register_artifact
from app.services.cowork_permissions import (
    CapabilityDeniedError,
    authorize_capability,
    authorize_path,
    create_session_root,
    grant_capability,
    list_capability_grants,
    revoke_session_root,
)
from app.services.request_identity import RequestIdentity
from app.services.runs import create_run, ensure_conversation

pytestmark = pytest.mark.integration


async def _owner_conversation(session: AsyncSession) -> UUID:
    conversation_id = await ensure_conversation(
        session, scope="local_owner", title="Cowork 测试"
    )
    await session.commit()
    return conversation_id


async def test_read_write_root_grants_office_without_shell_and_revokes_together(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    conversation_id = await _owner_conversation(db_session)
    root_path = tmp_path / "workspace"
    root_path.mkdir()
    word_path = root_path / "brief.docx"
    excel_path = root_path / "budget.xlsx"
    word_path.write_bytes(b"docx-placeholder")
    excel_path.write_bytes(b"xlsx-placeholder")

    root = await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(root_path),
        access_mode="read_write",
        label="项目资料",
    )
    await db_session.commit()

    grants = await list_capability_grants(
        db_session, conversation_id=conversation_id
    )
    assert {grant.capability for grant in grants} == {
        "filesystem.read",
        "filesystem.write",
        "office.word.edit",
        "office.excel.edit",
    }
    assert (
        await authorize_path(
            db_session,
            conversation_id=conversation_id,
            target_path=word_path,
            capability="office.word.edit",
        )
    ).root_id == root.id
    assert (
        await authorize_path(
            db_session,
            conversation_id=conversation_id,
            target_path=excel_path,
            capability="office.excel.edit",
        )
    ).target_path == excel_path.resolve()
    with pytest.raises(CapabilityDeniedError, match=r"shell\.execute"):
        await authorize_capability(
            db_session,
            conversation_id=conversation_id,
            capability="shell.execute",
        )
    with pytest.raises(CapabilityDeniedError, match=r"network\.read"):
        await authorize_capability(
            db_session,
            conversation_id=conversation_id,
            capability="network.read",
        )
    network_grant = await grant_capability(
        db_session,
        conversation_id=conversation_id,
        capability="network.read",
    )
    assert network_grant.session_root_id is None
    assert (
        await authorize_capability(
            db_session,
            conversation_id=conversation_id,
            capability="network.read",
        )
    ).id == network_grant.id

    assert await revoke_session_root(
        db_session, conversation_id=conversation_id, root_id=root.id
    )
    await db_session.commit()
    with pytest.raises(CapabilityDeniedError, match=r"office\.word\.edit"):
        await authorize_path(
            db_session,
            conversation_id=conversation_id,
            target_path=word_path,
            capability="office.word.edit",
        )


async def test_read_only_and_symlink_escape_fail_closed(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    conversation_id = await _owner_conversation(db_session)
    root_path = tmp_path / "read-only"
    root_path.mkdir()
    outside = tmp_path / "outside.docx"
    outside.write_bytes(b"outside")
    (root_path / "escape.docx").symlink_to(outside)
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(root_path),
        access_mode="read_only",
    )
    await db_session.commit()

    with pytest.raises(CapabilityDeniedError, match=r"filesystem\.write"):
        await authorize_path(
            db_session,
            conversation_id=conversation_id,
            target_path=root_path / "new.md",
            capability="filesystem.write",
        )
    with pytest.raises(CapabilityDeniedError, match=r"filesystem\.read"):
        await authorize_path(
            db_session,
            conversation_id=conversation_id,
            target_path=root_path / "escape.docx",
            capability="filesystem.read",
        )


async def test_nested_read_only_root_does_not_subtract_parent_write_grant(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    conversation_id = await _owner_conversation(db_session)
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    target = child / "note.md"
    target.write_text("content", encoding="utf-8")
    parent_root = await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(parent),
        access_mode="read_write",
    )
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(child),
        access_mode="read_only",
    )
    await db_session.commit()

    authorization = await authorize_path(
        db_session,
        conversation_id=conversation_id,
        target_path=target,
        capability="filesystem.write",
    )

    assert authorization.root_id == parent_root.id


async def test_cowork_api_grants_root_once_and_lists_artifacts(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    conversation_id = await _owner_conversation(db_session)
    canonical_tmp_path = str(tmp_path)

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app = create_app()
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_settings] = lambda: Settings(
        app_env="test", cowork_enabled=True
    )
    app.dependency_overrides[require_owner_identity] = lambda: RequestIdentity(
        scope="local_owner"
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            f"/api/v1/cowork/sessions/{conversation_id}/roots",
            json={"path": str(tmp_path), "access_mode": "read_write"},
        )
        grants = await client.get(
            f"/api/v1/cowork/sessions/{conversation_id}/grants"
        )
        await register_artifact(
            db_session,
            conversation_id=conversation_id,
            session_root_id=UUID(created.json()["id"]),
            kind="file",
            title="交付文档",
            uri="deliverables/result.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        await db_session.commit()
        artifacts = await client.get(
            f"/api/v1/cowork/sessions/{conversation_id}/artifacts"
        )

    assert created.status_code == 201
    assert created.json()["canonical_path"] == canonical_tmp_path
    assert grants.status_code == 200
    assert {item["capability"] for item in grants.json()["items"]} == {
        "filesystem.read",
        "filesystem.write",
        "office.word.edit",
        "office.excel.edit",
    }
    assert artifacts.status_code == 200
    assert len(artifacts.json()["items"]) == 1
    assert artifacts.json()["items"][0]["uri"].endswith(
        "/deliverables/result.docx"
    )


async def test_cowork_workflow_uses_existing_run_model(db_session: AsyncSession) -> None:
    conversation_id = await _owner_conversation(db_session)
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="整理目录内的季度报告",
        budget_tokens=20_000,
        budget_calls=20,
        budget_wall_ms=120_000,
        workflow_type="cowork",
    )
    await db_session.commit()

    assert run.workflow_type == "cowork"
