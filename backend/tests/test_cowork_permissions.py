from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from app.api.dependencies import require_owner_identity
from app.core.config import Settings, get_settings
from app.core.db import DbSession as AsyncSession
from app.core.db import get_db_session
from app.cowork.artifacts import register_artifact
from app.cowork.permissions import (
    CapabilityDeniedError,
    authorize_capability,
    authorize_path,
    authorize_scoped_capability,
    create_session_root,
    ensure_default_session_root,
    grant_capability,
    list_capability_grants,
    list_session_roots,
    revoke_capability_grant,
    revoke_session_root,
)
from app.main import create_app
from app.runstore.runs import create_run, ensure_conversation

pytestmark = pytest.mark.integration


async def _owner_conversation(session: AsyncSession) -> UUID:
    conversation_id = await ensure_conversation(session, title="Cowork 测试")
    await session.commit()
    return conversation_id


async def test_user_selected_root_precedes_managed_default(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    conversation_id = await _owner_conversation(db_session)
    selected = tmp_path / "selected"
    selected.mkdir()
    chosen = await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(selected),
        access_mode="read_write",
        label="用户项目",
    )
    managed = await ensure_default_session_root(
        db_session,
        conversation_id=conversation_id,
        workspace_path=tmp_path / "managed",
    )

    roots = await list_session_roots(db_session, conversation_id=conversation_id)

    assert [item.id for item in roots[:2]] == [chosen.id, managed.id]


async def test_read_write_root_grants_filesystem_without_shell_and_revokes_together(
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

    grants = await list_capability_grants(db_session, conversation_id=conversation_id)
    assert {grant.capability for grant in grants} == {
        "filesystem.read",
        "filesystem.write",
    }
    assert (
        await authorize_path(
            db_session,
            conversation_id=conversation_id,
            target_path=word_path,
            capability="filesystem.write",
        )
    ).root_id == root.id
    assert (
        await authorize_path(
            db_session,
            conversation_id=conversation_id,
            target_path=excel_path,
            capability="filesystem.write",
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
    with pytest.raises(CapabilityDeniedError, match=r"knowledge\.read"):
        await authorize_capability(
            db_session,
            conversation_id=conversation_id,
            capability="knowledge.read",
        )
    knowledge_grant = await grant_capability(
        db_session,
        conversation_id=conversation_id,
        capability="knowledge.read",
    )
    assert (
        await authorize_capability(
            db_session,
            conversation_id=conversation_id,
            capability="knowledge.read",
        )
    ).id == knowledge_grant.id
    assert await revoke_capability_grant(
        db_session,
        conversation_id=conversation_id,
        grant_id=knowledge_grant.id,
    )
    with pytest.raises(CapabilityDeniedError, match=r"knowledge\.read"):
        await authorize_capability(
            db_session,
            conversation_id=conversation_id,
            capability="knowledge.read",
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

    assert await revoke_session_root(db_session, conversation_id=conversation_id, root_id=root.id)
    await db_session.commit()
    with pytest.raises(CapabilityDeniedError, match=r"filesystem\.write"):
        await authorize_path(
            db_session,
            conversation_id=conversation_id,
            target_path=word_path,
            capability="filesystem.write",
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


async def test_network_grants_are_origin_or_domain_scoped_and_rechecked(
    db_session: AsyncSession,
    store_sql,
) -> None:
    conversation_id = await _owner_conversation(db_session)
    origin = await grant_capability(
        db_session,
        conversation_id=conversation_id,
        capability="network.fetch",
        resource_scope="https://api.example.com/v1",
    )
    domain = await grant_capability(
        db_session,
        conversation_id=conversation_id,
        capability="network.fetch",
        resource_scope="domain:trusted.example",
    )

    assert origin.resource_scope == "origin:https://api.example.com"
    assert (
        await authorize_scoped_capability(
            db_session,
            conversation_id=conversation_id,
            capability="network.fetch",
            target="https://api.example.com/other",
        )
    ).id == origin.id
    assert (
        await authorize_scoped_capability(
            db_session,
            conversation_id=conversation_id,
            capability="network.fetch",
            target="https://cdn.trusted.example/file",
        )
    ).id == domain.id
    with pytest.raises(CapabilityDeniedError, match=r"network\.fetch"):
        await authorize_scoped_capability(
            db_session,
            conversation_id=conversation_id,
            capability="network.fetch",
            target="https://trusted.example.evil.test/exfiltrate",
        )

    store_sql(
        "UPDATE capability_grants SET expires_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", str(origin.id)),
    )
    with pytest.raises(CapabilityDeniedError, match=r"network\.fetch"):
        await authorize_scoped_capability(
            db_session,
            conversation_id=conversation_id,
            capability="network.fetch",
            target="https://api.example.com/after-expiry",
        )
    assert await revoke_capability_grant(
        db_session,
        conversation_id=conversation_id,
        grant_id=domain.id,
    )
    with pytest.raises(CapabilityDeniedError, match=r"network\.fetch"):
        await authorize_scoped_capability(
            db_session,
            conversation_id=conversation_id,
            capability="network.fetch",
            target="https://trusted.example/after-revoke",
        )


async def test_path_authorization_is_rechecked_after_symlink_swap(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    conversation_id = await _owner_conversation(db_session)
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "result.txt"
    target.write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(root),
        access_mode="read_write",
    )

    first = await authorize_path(
        db_session,
        conversation_id=conversation_id,
        target_path=target,
        capability="filesystem.write",
    )
    assert first.target_path == target.resolve()
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(CapabilityDeniedError, match=r"filesystem\.write"):
        await authorize_path(
            db_session,
            conversation_id=conversation_id,
            target_path=target,
            capability="filesystem.write",
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
    app.dependency_overrides[get_settings] = lambda: Settings(app_env="test", cowork_enabled=True)
    app.dependency_overrides[require_owner_identity] = lambda: None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            f"/api/v1/cowork/sessions/{conversation_id}/roots",
            json={"path": str(tmp_path), "access_mode": "read_write"},
        )
        grants = await client.get(f"/api/v1/cowork/sessions/{conversation_id}/grants")
        retired_grant = await client.post(
            f"/api/v1/cowork/sessions/{conversation_id}/grants",
            json={
                "capability": "office.word.edit",
                "session_root_id": created.json()["id"],
            },
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
        artifacts = await client.get(f"/api/v1/cowork/sessions/{conversation_id}/artifacts")

    assert created.status_code == 201
    assert created.json()["canonical_path"] == canonical_tmp_path
    assert grants.status_code == 200
    assert retired_grant.status_code == 422
    assert {item["capability"] for item in grants.json()["items"]} == {
        "filesystem.read",
        "filesystem.write",
    }
    assert artifacts.status_code == 200
    assert len(artifacts.json()["items"]) == 1
    assert artifacts.json()["items"][0]["uri"].endswith("/deliverables/result.docx")


async def test_artifact_preview_ignores_model_mime_and_sandboxes_text(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    conversation_id = await _owner_conversation(db_session)
    root = await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    payload = tmp_path / "payload.xml"
    payload.write_text(
        "<script>window.top.document.body.textContent='owned'</script>", encoding="utf-8"
    )
    artifact = await register_artifact(
        db_session,
        conversation_id=conversation_id,
        session_root_id=root.id,
        kind="file",
        title="payload.xml",
        uri=str(payload),
        mime_type="text/html",
        meta={
            "diff": {
                "schema_version": 1,
                "available": True,
                "format": "unified",
                "view": "text",
                "created": False,
                "before_sha256": "a" * 64,
                "after_sha256": "b" * 64,
                "added_lines": 1,
                "removed_lines": 1,
                "truncated": False,
                "text": "--- before\n+++ after\n-old\n+new",
                "reason": None,
            }
        },
    )
    await db_session.commit()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app = create_app()
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_settings] = lambda: Settings(app_env="test", cowork_enabled=True)
    app.dependency_overrides[require_owner_identity] = lambda: None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/v1/cowork/artifacts/{artifact.id}/preview")
        diff = await client.get(f"/api/v1/cowork/artifacts/{artifact.id}/diff")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]
    assert "<script>" not in response.text
    assert "&lt;script&gt;" in response.text
    assert diff.status_code == 200
    assert diff.json()["added_lines"] == 1
    assert diff.json()["removed_lines"] == 1
    assert diff.json()["text"].endswith("-old\n+new")


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
