import json
from pathlib import Path
from uuid import UUID

import httpx
import pymupdf
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.core.db import get_db_session
from app.main import create_app
from app.services.demo_sessions import resolve_demo_session
from app.services.runs import append_events, create_run, ensure_conversation

pytestmark = pytest.mark.integration


async def _seed_local_version(
    session: AsyncSession,
    *,
    root: Path,
    source_uri: str,
    title: str,
) -> UUID:
    source_id = uuid7()
    document_id = uuid7()
    version_id = uuid7()
    async with session.begin():
        await session.execute(
            text(
                """
                INSERT INTO sources (id, kind, name, config)
                VALUES (:id, 'local_dir', 'preview-test', CAST(:config AS jsonb))
                """
            ),
            {"id": source_id, "config": json.dumps({"root": str(root)})},
        )
        await session.execute(
            text(
                """
                INSERT INTO documents (id, source_id, source_uri, title, doc_type)
                VALUES (:id, :source_id, :source_uri, :title, 'note')
                """
            ),
            {
                "id": document_id,
                "source_id": source_id,
                "source_uri": source_uri,
                "title": title,
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO document_versions (
                    id, document_id, version_no, content_hash, parser, parser_version,
                    full_text, parse_status, valid_from, activated_at,
                    embedding_model, embedding_provider, embedding_revision
                ) VALUES (
                    :id, :document_id, 1, 'preview-hash', 'test', '1',
                    'preview', 'done', now(), now(), 'test', 'test', '1'
                )
                """
            ),
            {"id": version_id, "document_id": document_id},
        )
    return version_id


async def _grant_version_access(session: AsyncSession, version_id: UUID) -> str:
    resolved = await resolve_demo_session(session, cookie_token=None, ttl_s=3600)
    conversation_id = await ensure_conversation(
        session,
        scope="demo",
        demo_session_id=resolved.session.id,
    )
    run = await create_run(
        session,
        conversation_id=conversation_id,
        goal="preview",
        budget_tokens=10,
        budget_calls=1,
        budget_wall_ms=1000,
    )
    await append_events(
        session,
        run_id=run.id,
        events=[("citation", {"version_id": str(version_id)})],
    )
    await session.commit()
    assert resolved.cookie_token is not None
    return resolved.cookie_token


def _client(db_session: AsyncSession, *, cookie: str | None = None) -> httpx.AsyncClient:
    async def override_session():
        yield db_session

    app = create_app()
    app.dependency_overrides[get_db_session] = override_session
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    if cookie is not None:
        client.cookies.set("workpilot_session", cookie, domain="test.local", path="/")
    return client


async def test_document_file_is_served_inline_from_registered_source(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    source = root / "note.md"
    source.write_text("# Note\n\nEvidence line.", encoding="utf-8")
    version_id = await _seed_local_version(
        db_session, root=root, source_uri="note.md", title="Note"
    )
    cookie = await _grant_version_access(db_session, version_id)

    async with _client(db_session, cookie=cookie) as client:
        response = await client.get(f"/api/v1/documents/{version_id}/file")

    assert response.status_code == 200
    assert response.text == "# Note\n\nEvidence line."
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.headers["content-disposition"].startswith("inline")
    assert response.headers["cache-control"] == "private, max-age=3600"


async def test_document_pdf_page_is_rendered_for_bbox_overlay(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    source = root / "paper.pdf"
    document = pymupdf.open()
    page = document.new_page(width=320, height=480)
    page.insert_text((40, 80), "Evidence")
    document.save(source)
    document.close()
    version_id = await _seed_local_version(
        db_session, root=root, source_uri="paper.pdf", title="Paper"
    )
    cookie = await _grant_version_access(db_session, version_id)

    async with _client(db_session, cookie=cookie) as client:
        response = await client.get(f"/api/v1/documents/{version_id}/pages/1.png")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")


async def test_document_preview_rejects_path_escape(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    (tmp_path / "outside.md").write_text("private", encoding="utf-8")
    version_id = await _seed_local_version(
        db_session, root=root, source_uri="../outside.md", title="Outside"
    )
    cookie = await _grant_version_access(db_session, version_id)

    async with _client(db_session, cookie=cookie) as client:
        response = await client.get(f"/api/v1/documents/{version_id}/file")

    assert response.status_code == 404
    assert "越过资料根目录" in response.json()["detail"]


async def test_document_preview_is_hidden_from_other_session(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    (root / "note.md").write_text("private evidence", encoding="utf-8")
    version_id = await _seed_local_version(
        db_session, root=root, source_uri="note.md", title="Note"
    )
    owner_cookie = await _grant_version_access(db_session, version_id)

    async with _client(db_session, cookie=owner_cookie) as owner, _client(db_session) as intruder:
        assert (await owner.get(f"/api/v1/documents/{version_id}/file")).status_code == 200
        assert (await intruder.get(f"/api/v1/documents/{version_id}/file")).status_code == 404
        assert (
            await intruder.get(f"/api/v1/documents/{version_id}/pages/1.png")
        ).status_code == 404
