import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.llm.gateway import ModelGateway
from app.services.local_dir import (
    SyncProgress,
    _scan_files,
    register_local_dir,
    sync_local_dir,
)
from tests.fakes import DeterministicProvider


@pytest.mark.integration
async def test_local_dir_sync_add_skip_update_delete_and_restore(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    note = library / "note.md"
    note.write_text("# Note\n\nfirst version", encoding="utf-8")
    settings = Settings.model_validate(
        {"local_library_path": library, "pdf_parser_mode": "pymupdf"}
    )
    gateway = ModelGateway(DeterministicProvider(), embedding_dimensions=1024)
    source = await register_local_dir(
        db_session, requested_root=Path("."), allowed_root=library, name="fixture"
    )

    first = await sync_local_dir(
        db_session,
        gateway,
        source_id=source.id,
        allowed_root=library,
        settings=settings,
    )
    second = await sync_local_dir(
        db_session,
        gateway,
        source_id=source.id,
        allowed_root=library,
        settings=settings,
    )
    original_stat = note.stat()
    os.utime(
        note,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 1_000_000_000),
    )
    touched = await sync_local_dir(
        db_session,
        gateway,
        source_id=source.id,
        allowed_root=library,
        settings=settings,
    )
    note.write_text("# Note\n\nsecond version with changed size", encoding="utf-8")
    third = await sync_local_dir(
        db_session,
        gateway,
        source_id=source.id,
        allowed_root=library,
        settings=settings,
    )
    note.unlink()
    fourth = await sync_local_dir(
        db_session,
        gateway,
        source_id=source.id,
        allowed_root=library,
        settings=settings,
    )

    assert (first.added, first.failed) == (1, 0)
    assert (second.skipped, second.updated) == (1, 0)
    assert (touched.skipped, touched.updated) == (1, 0)
    assert (third.updated, third.failed) == (1, 0)
    assert fourth.deleted == 1
    states = (
        (
            await db_session.execute(
                text(
                    """
                    SELECT d.deleted_at, count(v.id) AS versions,
                           bool_or(c.is_searchable) AS any_searchable
                    FROM documents d
                    JOIN document_versions v ON v.document_id=d.id
                    JOIN chunks c ON c.version_id=v.id
                    WHERE d.source_id=:source_id
                    GROUP BY d.id
                    """
                ),
                {"source_id": source.id},
            )
        )
        .mappings()
        .one()
    )
    assert states["deleted_at"] is not None
    assert states["versions"] == 2
    assert states["any_searchable"] is False
    await db_session.rollback()

    note.write_text("# Note\n\nsecond version with changed size", encoding="utf-8")
    restored = await sync_local_dir(
        db_session,
        gateway,
        source_id=source.id,
        allowed_root=library,
        settings=settings,
    )
    restored_state = (
        (
            await db_session.execute(
                text(
                    """
                    SELECT d.deleted_at, count(v.id) AS versions,
                           bool_or(c.is_searchable) AS any_searchable
                    FROM documents d
                    JOIN document_versions v ON v.document_id=d.id
                    JOIN chunks c ON c.version_id=v.id
                    WHERE d.source_id=:source_id
                    GROUP BY d.id
                    """
                ),
                {"source_id": source.id},
            )
        )
        .mappings()
        .one()
    )
    assert restored.added == 1
    assert restored_state["deleted_at"] is None
    assert restored_state["versions"] == 2
    assert restored_state["any_searchable"] is True


def test_local_dir_scan_rejects_symlink(tmp_path: Path) -> None:
    library = tmp_path / "library"
    outside = tmp_path / "outside.md"
    library.mkdir()
    outside.write_text("# outside", encoding="utf-8")
    (library / "escape.md").symlink_to(outside)

    files, failures = _scan_files(library)

    assert files == []
    assert len(failures) == 1
    assert failures[0].source_uri == "escape.md"
    assert "符号链接" in failures[0].error


@pytest.mark.integration
async def test_local_dir_sync_isolates_a_broken_pdf(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    (library / "good.md").write_text("# Good\n\nsearchable evidence", encoding="utf-8")
    (library / "broken.pdf").write_bytes(b"not a pdf")
    settings = Settings.model_validate(
        {"local_library_path": library, "pdf_parser_mode": "pymupdf"}
    )
    gateway = ModelGateway(DeterministicProvider(), embedding_dimensions=1024)
    source = await register_local_dir(
        db_session, requested_root=Path("."), allowed_root=library, name="fixture"
    )

    result = await sync_local_dir(
        db_session,
        gateway,
        source_id=source.id,
        allowed_root=library,
        settings=settings,
    )

    assert (result.added, result.failed) == (1, 1)
    assert result.failures[0].source_uri == "broken.pdf"
    rows = (
        (
            await db_session.execute(
                text("SELECT source_uri, deleted_at FROM documents WHERE source_id=:source_id"),
                {"source_id": source.id},
            )
        )
        .mappings()
        .all()
    )
    assert rows == [{"source_uri": "good.md", "deleted_at": None}]


@pytest.mark.integration
async def test_local_dir_sync_reports_progress_for_every_file(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    (library / "good.md").write_text("# Good\n\nsearchable evidence", encoding="utf-8")
    (library / "broken.pdf").write_bytes(b"not a pdf")
    settings = Settings.model_validate(
        {"local_library_path": library, "pdf_parser_mode": "pymupdf"}
    )
    gateway = ModelGateway(DeterministicProvider(), embedding_dimensions=1024)
    source = await register_local_dir(
        db_session, requested_root=Path("."), allowed_root=library, name="fixture"
    )

    first_progress: list[SyncProgress] = []
    await sync_local_dir(
        db_session,
        gateway,
        source_id=source.id,
        allowed_root=library,
        settings=settings,
        on_progress=first_progress.append,
    )
    second_progress: list[SyncProgress] = []
    await sync_local_dir(
        db_session,
        gateway,
        source_id=source.id,
        allowed_root=library,
        settings=settings,
        on_progress=second_progress.append,
    )

    # 失败文件也必须上报, 否则数小时的批量导入会在中途"看起来卡住"。
    assert [item.index for item in first_progress] == [1, 2]
    assert {item.total for item in first_progress} == {2}
    assert {item.source_uri: item.action for item in first_progress} == {
        "broken.pdf": "failed",
        "good.md": "added",
    }
    broken = next(item for item in first_progress if item.source_uri == "broken.pdf")
    assert broken.error is not None
    assert all(item.elapsed_s >= 0 for item in first_progress)
    # 第二次只有失败文件会重试, 已入库文件走增量游标跳过。
    assert {item.source_uri: item.action for item in second_progress} == {
        "broken.pdf": "failed",
        "good.md": "skipped",
    }


@pytest.mark.integration
async def test_local_dir_sync_rebuilds_when_chunk_configuration_changes(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    (library / "long.md").write_text("# Long\n\n" + "evidence " * 500, encoding="utf-8")
    settings = Settings.model_validate(
        {"local_library_path": library, "pdf_parser_mode": "pymupdf"}
    )
    gateway = ModelGateway(DeterministicProvider(), embedding_dimensions=1024)
    source = await register_local_dir(
        db_session, requested_root=Path("."), allowed_root=library, name="fixture"
    )

    first = await sync_local_dir(
        db_session,
        gateway,
        source_id=source.id,
        allowed_root=library,
        settings=settings,
        max_chunk_chars=2000,
    )
    rebuilt = await sync_local_dir(
        db_session,
        gateway,
        source_id=source.id,
        allowed_root=library,
        settings=settings,
        max_chunk_chars=500,
    )

    version_count = (
        await db_session.execute(
            text(
                """
                SELECT count(*)
                FROM document_versions v
                JOIN documents d ON d.id=v.document_id
                WHERE d.source_id=:source_id
                """
            ),
            {"source_id": source.id},
        )
    ).scalar_one()
    signatures = (
        (
            await db_session.execute(
                text(
                    """
                    SELECT parse_meta->>'ingest_signature' AS signature
                    FROM document_versions v
                    JOIN documents d ON d.id=v.document_id
                    WHERE d.source_id=:source_id
                    ORDER BY version_no
                    """
                ),
                {"source_id": source.id},
            )
        )
        .scalars()
        .all()
    )

    assert first.added == 1
    assert rebuilt.updated == 1
    assert version_count == 2
    assert len(set(signatures)) == 2
