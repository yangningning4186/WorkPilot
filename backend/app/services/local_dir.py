import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.core.config import Settings
from app.llm.gateway import ModelGateway
from app.services.document_ingestion import IngestionResult
from app.services.markdown_ingestion import LibraryPathError, ingest_markdown_file
from app.services.pdf_ingestion import ingest_pdf_file

SUPPORTED_SUFFIXES = {".md", ".markdown", ".pdf"}


class SourceNotFoundError(LookupError):
    pass


class SourceBusyError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalDirSource:
    id: UUID
    name: str
    root: Path
    sync_status: str
    sync_error: str | None
    document_count: int


@dataclass(frozen=True)
class SyncFailure:
    source_uri: str
    error: str


@dataclass(frozen=True)
class LocalDirSyncResult:
    source_id: UUID
    added: int
    updated: int
    skipped: int
    deleted: int
    failed: int
    failures: list[SyncFailure]


@dataclass(frozen=True)
class ScannedFile:
    path: Path
    source_uri: str
    size_bytes: int
    mtime_ns: int


async def register_local_dir(
    session: AsyncSession,
    *,
    requested_root: Path,
    allowed_root: Path,
    name: str | None = None,
) -> LocalDirSource:
    root = _resolve_registered_root(requested_root, allowed_root)
    async with session.begin():
        row = (
            (
                await session.execute(
                    text(
                        """
                        INSERT INTO sources (id, kind, name, config)
                        VALUES (:id, 'local_dir', :name, CAST(:config AS jsonb))
                        ON CONFLICT ((config->>'root')) WHERE kind='local_dir'
                        DO UPDATE SET name=EXCLUDED.name, enabled=true
                        RETURNING id, name, config, sync_status, sync_error
                        """
                    ),
                    {
                        "id": uuid7(),
                        "name": (name or root.name or "library").strip() or root.name or "library",
                        "config": json.dumps({"root": str(root)}),
                    },
                )
            )
            .mappings()
            .one()
        )
    return await get_local_dir_source(session, row["id"])


async def get_local_dir_source(session: AsyncSession, source_id: UUID) -> LocalDirSource:
    row = (
        (
            await session.execute(
                text(
                    """
                    SELECT s.id, s.name, s.config, s.sync_status, s.sync_error,
                           count(d.id) FILTER (WHERE d.deleted_at IS NULL) AS document_count
                    FROM sources s
                    LEFT JOIN documents d ON d.source_id=s.id
                    WHERE s.id=:source_id AND s.kind='local_dir'
                    GROUP BY s.id
                    """
                ),
                {"source_id": source_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    await session.rollback()
    if row is None:
        raise SourceNotFoundError(str(source_id))
    return LocalDirSource(
        id=row["id"],
        name=row["name"],
        root=Path(row["config"]["root"]),
        sync_status=row["sync_status"],
        sync_error=row["sync_error"],
        document_count=row["document_count"],
    )


async def sync_local_dir(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    source_id: UUID,
    allowed_root: Path,
    settings: Settings,
    max_chunk_chars: int = 2000,
) -> LocalDirSyncResult:
    root = await _begin_sync(session, source_id, allowed_root)
    failures: list[SyncFailure] = []
    counters = {"added": 0, "updated": 0, "skipped": 0}
    try:
        files, scan_failures = await asyncio.to_thread(_scan_files, root)
        failures.extend(scan_failures)
        entries = await _load_entries(session, source_id)
        seen = {item.source_uri for item in files} | {item.source_uri for item in scan_failures}
        for item in files:
            previous = entries.get(item.source_uri)
            if (
                previous is not None
                and previous["sync_status"] == "synced"
                and previous["size_bytes"] == item.size_bytes
                and previous["mtime_ns"] == item.mtime_ns
            ):
                previous_hash = previous["content_hash"]
                await _record_entry(
                    session,
                    source_id,
                    item,
                    previous_hash if isinstance(previous_hash, str) else None,
                    None,
                )
                counters["skipped"] += 1
                continue
            try:
                file_hash = await asyncio.to_thread(_sha256_file, item.path)
                if (
                    previous is not None
                    and previous["sync_status"] == "synced"
                    and previous["content_hash"] == file_hash
                ):
                    await _record_entry(session, source_id, item, file_hash, None)
                    counters["skipped"] += 1
                    continue
                result = await _ingest_scanned_file(
                    session,
                    gateway,
                    source_id=source_id,
                    root=root,
                    item=item,
                    settings=settings,
                    max_chunk_chars=max_chunk_chars,
                )
                await _record_entry(session, source_id, item, file_hash, None)
                if previous is None:
                    counters["added"] += 1
                elif result.unchanged:
                    counters["skipped"] += 1
                else:
                    counters["updated"] += 1
            except Exception as error:
                await session.rollback()
                message = str(error)[:2000]
                failures.append(SyncFailure(source_uri=item.source_uri, error=message))
                await _record_entry(session, source_id, item, None, message)

        for failure in scan_failures:
            await _record_scan_failure(session, source_id, failure)
        deleted = await _mark_missing_documents_deleted(
            session, source_id=source_id, missing_uris=set(entries) - seen
        )
        await _finish_sync(session, source_id, failures)
        return LocalDirSyncResult(
            source_id=source_id,
            added=counters["added"],
            updated=counters["updated"],
            skipped=counters["skipped"],
            deleted=deleted,
            failed=len(failures),
            failures=failures,
        )
    except Exception as error:
        await session.rollback()
        await _fail_sync(session, source_id, error)
        raise


def _resolve_registered_root(requested_root: Path, allowed_root: Path) -> Path:
    allowed = allowed_root.expanduser().resolve()
    candidate = requested_root if requested_root.is_absolute() else allowed / requested_root
    root = candidate.expanduser().resolve()
    if not root.is_relative_to(allowed):
        raise LibraryPathError("local_dir 必须位于 LOCAL_LIBRARY_PATH 内")
    if not root.is_dir():
        raise FileNotFoundError(root)
    return root


def _scan_files(root: Path) -> tuple[list[ScannedFile], list[SyncFailure]]:
    files: list[ScannedFile] = []
    failures: list[SyncFailure] = []
    resolved_paths: set[Path] = set()
    for path in sorted(root.rglob("*")):
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        source_uri = path.relative_to(root).as_posix()
        try:
            if path.is_symlink():
                raise LibraryPathError("不允许同步符号链接文件")
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise LibraryPathError("符号链接目标越过资料目录")
            if not resolved.is_file():
                continue
            if resolved in resolved_paths:
                raise LibraryPathError("文件与另一个路径指向同一目标")
            resolved_paths.add(resolved)
            stat = resolved.stat()
            files.append(
                ScannedFile(
                    path=resolved,
                    source_uri=source_uri,
                    size_bytes=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                )
            )
        except (OSError, ValueError) as error:
            failures.append(SyncFailure(source_uri=source_uri, error=str(error)))
    return files, failures


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _begin_sync(session: AsyncSession, source_id: UUID, allowed_root: Path) -> Path:
    async with session.begin():
        row = (
            (
                await session.execute(
                    text(
                        """
                        UPDATE sources
                        SET sync_status='syncing', sync_error=NULL
                        WHERE id=:source_id AND kind='local_dir' AND enabled=true
                          AND sync_status <> 'syncing'
                        RETURNING config
                        """
                    ),
                    {"source_id": source_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            exists = (
                await session.execute(
                    text(
                        "SELECT sync_status FROM sources WHERE id=:source_id AND kind='local_dir'"
                    ),
                    {"source_id": source_id},
                )
            ).scalar_one_or_none()
            if exists == "syncing":
                raise SourceBusyError("该 source 正在同步")
            raise SourceNotFoundError(str(source_id))
        # Resolve inside the transaction so an invalid path rolls back `syncing`.
        return _resolve_registered_root(Path(row["config"]["root"]), allowed_root)


async def _load_entries(session: AsyncSession, source_id: UUID) -> dict[str, dict[str, object]]:
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT source_uri, size_bytes, mtime_ns, content_hash, sync_status
                    FROM source_sync_entries WHERE source_id=:source_id
                    """
                ),
                {"source_id": source_id},
            )
        )
        .mappings()
        .all()
    )
    await session.rollback()
    return {row["source_uri"]: dict(row) for row in rows}


async def _ingest_scanned_file(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    source_id: UUID,
    root: Path,
    item: ScannedFile,
    settings: Settings,
    max_chunk_chars: int,
) -> IngestionResult:
    if item.path.suffix.lower() in {".md", ".markdown"}:
        return await ingest_markdown_file(
            session,
            gateway,
            path=item.path,
            library_root=root,
            max_chunk_chars=max_chunk_chars,
            source_id=source_id,
        )
    return await ingest_pdf_file(
        session,
        gateway,
        path=item.path,
        library_root=root,
        max_chunk_chars=max_chunk_chars,
        source_id=source_id,
        timeout_s=settings.pdf_parse_timeout_s,
        max_pages=settings.pdf_max_pages,
        max_bytes=settings.pdf_max_bytes,
        memory_mb=settings.pdf_worker_memory_mb,
        cpu_seconds=settings.pdf_worker_cpu_s,
    )


async def _record_entry(
    session: AsyncSession,
    source_id: UUID,
    item: ScannedFile,
    content_hash: str | None,
    error: str | None,
) -> None:
    async with session.begin():
        await session.execute(
            text(
                """
                INSERT INTO source_sync_entries
                    (source_id, source_uri, size_bytes, mtime_ns, content_hash, sync_status,
                     sync_error, last_seen_at)
                VALUES
                    (:source_id, :source_uri, :size_bytes, :mtime_ns, :content_hash,
                     :sync_status, :sync_error, now())
                ON CONFLICT (source_id, source_uri) DO UPDATE SET
                    size_bytes=EXCLUDED.size_bytes,
                    mtime_ns=EXCLUDED.mtime_ns,
                    content_hash=COALESCE(EXCLUDED.content_hash, source_sync_entries.content_hash),
                    sync_status=EXCLUDED.sync_status,
                    sync_error=EXCLUDED.sync_error,
                    last_seen_at=now(),
                    updated_at=now()
                """
            ),
            {
                "source_id": source_id,
                "source_uri": item.source_uri,
                "size_bytes": item.size_bytes,
                "mtime_ns": item.mtime_ns,
                "content_hash": content_hash,
                "sync_status": "failed" if error else "synced",
                "sync_error": error,
            },
        )


async def _record_scan_failure(
    session: AsyncSession, source_id: UUID, failure: SyncFailure
) -> None:
    item = ScannedFile(path=Path(), source_uri=failure.source_uri, size_bytes=0, mtime_ns=0)
    await _record_entry(session, source_id, item, None, failure.error)


async def _mark_missing_documents_deleted(
    session: AsyncSession, *, source_id: UUID, missing_uris: set[str]
) -> int:
    if not missing_uris:
        return 0
    async with session.begin():
        document_ids = (
            (
                await session.execute(
                    text(
                        """
                        UPDATE documents
                        SET deleted_at=now()
                        WHERE source_id=:source_id AND source_uri=ANY(:uris)
                          AND deleted_at IS NULL
                        RETURNING id
                        """
                    ),
                    {"source_id": source_id, "uris": list(missing_uris)},
                )
            )
            .scalars()
            .all()
        )
        if document_ids:
            await session.execute(
                text(
                    """
                    UPDATE chunks SET is_searchable=false
                    WHERE version_id IN (
                      SELECT id FROM document_versions WHERE document_id=ANY(:document_ids)
                    )
                    """
                ),
                {"document_ids": document_ids},
            )
        await session.execute(
            text(
                "DELETE FROM source_sync_entries "
                "WHERE source_id=:source_id AND source_uri=ANY(:uris)"
            ),
            {"source_id": source_id, "uris": list(missing_uris)},
        )
    return len(document_ids)


async def _finish_sync(session: AsyncSession, source_id: UUID, failures: list[SyncFailure]) -> None:
    async with session.begin():
        await session.execute(
            text(
                """
                UPDATE sources
                SET last_sync_at=now(), sync_status=:status, sync_error=:error
                WHERE id=:source_id
                """
            ),
            {
                "source_id": source_id,
                "status": "failed" if failures else "idle",
                "error": f"{len(failures)} 个文件失败" if failures else None,
            },
        )


async def _fail_sync(session: AsyncSession, source_id: UUID, error: Exception) -> None:
    async with session.begin():
        await session.execute(
            text("UPDATE sources SET sync_status='failed', sync_error=:error WHERE id=:source_id"),
            {"source_id": source_id, "error": str(error)[:4000]},
        )
