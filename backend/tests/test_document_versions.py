from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.services.document_versions import (
    VersionNotReadyError,
    activate_document_version,
    create_candidate_version,
)

pytestmark = pytest.mark.integration


async def _seed_document(session: AsyncSession) -> UUID:
    source_id = uuid7()
    document_id = uuid7()
    async with session.begin():
        await session.execute(
            text(
                "INSERT INTO sources (id, kind, name, config) "
                "VALUES (:id, 'local_dir', 'test', '{}'::jsonb)"
            ),
            {"id": source_id},
        )
        await session.execute(
            text(
                """
                INSERT INTO documents (id, source_id, source_uri, title, doc_type)
                VALUES (:id, :source_id, 'test.md', 'Test', 'note')
                """
            ),
            {"id": document_id, "source_id": source_id},
        )
    return document_id


async def _make_ready(session: AsyncSession, version_id: UUID, content: str) -> None:
    block_id = uuid7()
    chunk_id = uuid7()
    async with session.begin():
        await session.execute(
            text(
                """
                UPDATE document_versions
                SET parse_status = 'done', full_text = :content
                WHERE id = :version_id
                """
            ),
            {"version_id": version_id, "content": content},
        )
        await session.execute(
            text(
                """
                INSERT INTO parsed_blocks
                    (id, version_id, block_idx, block_type, text, char_start, char_end)
                VALUES (:id, :version_id, 0, 'paragraph', :content, 0, :length)
                """
            ),
            {
                "id": block_id,
                "version_id": version_id,
                "content": content,
                "length": len(content),
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO chunks
                    (id, version_id, strategy, chunk_index, content, content_tokens,
                     block_start_idx, block_end_idx, char_start, char_end,
                     dominant_block_type, embedding, doc_type, embedding_model,
                     embedding_provider, embedding_revision)
                VALUES
                    (:id, :version_id, 'heading', 0, :content, 1,
                     0, 0, 0, :length, 'paragraph',
                     array_fill(0::real, ARRAY[1024])::vector, 'note',
                     'legacy-unknown', 'legacy-unknown', 'legacy-unknown')
                """
            ),
            {
                "id": chunk_id,
                "version_id": version_id,
                "content": content,
                "length": len(content),
            },
        )


async def test_activation_switches_version_and_visibility_atomically(
    db_session: AsyncSession,
) -> None:
    document_id = await _seed_document(db_session)
    v1 = await create_candidate_version(
        db_session,
        document_id=document_id,
        content_hash="hash-v1",
        parser="markdown",
        parser_version="1",
    )
    await _make_ready(db_session, v1.id, "version one")
    assert await activate_document_version(db_session, v1.id) is True

    v2 = await create_candidate_version(
        db_session,
        document_id=document_id,
        content_hash="hash-v2",
        parser="markdown",
        parser_version="1",
    )
    await _make_ready(db_session, v2.id, "version two")
    assert await activate_document_version(db_session, v2.id) is True

    rows = (
        await db_session.execute(
            text(
                """
                SELECT v.version_no, v.invalid_at, c.is_searchable
                FROM document_versions v
                JOIN chunks c ON c.version_id = v.id
                ORDER BY v.version_no
                """
            )
        )
    ).all()
    assert rows[0].invalid_at is not None
    assert rows[0].is_searchable is False
    assert rows[1].invalid_at is None
    assert rows[1].is_searchable is True


async def test_failed_candidate_keeps_old_version_searchable(db_session: AsyncSession) -> None:
    document_id = await _seed_document(db_session)
    current = await create_candidate_version(
        db_session,
        document_id=document_id,
        content_hash="current",
        parser="markdown",
        parser_version="1",
    )
    await _make_ready(db_session, current.id, "current")
    await activate_document_version(db_session, current.id)

    failed = await create_candidate_version(
        db_session,
        document_id=document_id,
        content_hash="failed",
        parser="markdown",
        parser_version="1",
    )
    async with db_session.begin():
        await db_session.execute(
            text("UPDATE document_versions SET parse_status='failed' WHERE id=:id"),
            {"id": failed.id},
        )

    with pytest.raises(VersionNotReadyError):
        await activate_document_version(db_session, failed.id)

    searchable = (
        await db_session.execute(
            text("SELECT is_searchable FROM chunks WHERE version_id=:id"),
            {"id": current.id},
        )
    ).scalar_one()
    assert searchable is True


async def test_slow_old_candidate_cannot_replace_newer_version(db_session: AsyncSession) -> None:
    document_id = await _seed_document(db_session)
    slow = await create_candidate_version(
        db_session,
        document_id=document_id,
        content_hash="slow-v1",
        parser="markdown",
        parser_version="1",
    )
    fast = await create_candidate_version(
        db_session,
        document_id=document_id,
        content_hash="fast-v2",
        parser="markdown",
        parser_version="1",
    )
    await _make_ready(db_session, fast.id, "fast")
    await activate_document_version(db_session, fast.id)
    await _make_ready(db_session, slow.id, "slow")

    assert await activate_document_version(db_session, slow.id) is False
    states = (
        (
            await db_session.execute(
                text(
                    """
                SELECT id, parse_status, activated_at
                FROM document_versions
                WHERE document_id=:document_id
                """
                ),
                {"document_id": document_id},
            )
        )
        .mappings()
        .all()
    )
    by_id = {row["id"]: row for row in states}
    assert by_id[slow.id]["parse_status"] == "superseded"
    assert by_id[slow.id]["activated_at"] is None
    assert by_id[fast.id]["activated_at"] is not None


async def test_updated_at_is_maintained_by_database(db_session: AsyncSession) -> None:
    document_id = await _seed_document(db_session)
    before = (
        await db_session.execute(
            text("SELECT updated_at FROM documents WHERE id=:id"), {"id": document_id}
        )
    ).scalar_one()
    await db_session.commit()

    async with db_session.begin():
        await db_session.execute(text("SELECT pg_sleep(0.01)"))
        await db_session.execute(
            text("UPDATE documents SET title='Updated' WHERE id=:id"), {"id": document_id}
        )

    after = (
        await db_session.execute(
            text("SELECT updated_at FROM documents WHERE id=:id"), {"id": document_id}
        )
    ).scalar_one()
    assert after > before
