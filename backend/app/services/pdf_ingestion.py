import asyncio
import hashlib
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest.pdf import parse_pdf_in_subprocess
from app.llm.gateway import ModelGateway
from app.services.document_ingestion import IngestionResult, persist_parsed_document
from app.services.markdown_ingestion import LibraryPathError


async def ingest_pdf_file(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    path: Path,
    library_root: Path,
    max_chunk_chars: int = 2000,
    source_id: UUID | None = None,
    timeout_s: float = 120,
    max_pages: int = 500,
    max_bytes: int = 50 * 1024 * 1024,
    memory_mb: int = 2048,
    cpu_seconds: int = 120,
) -> IngestionResult:
    root, resolved = await asyncio.to_thread(_resolve_pdf, path, library_root, max_bytes)
    content_hash = await asyncio.to_thread(_sha256_file, resolved)
    parsed = await parse_pdf_in_subprocess(
        resolved,
        timeout_s=timeout_s,
        max_pages=max_pages,
        memory_mb=memory_mb,
        cpu_seconds=cpu_seconds,
    )
    return await persist_parsed_document(
        session,
        gateway,
        library_root=root,
        source_uri=resolved.relative_to(root).as_posix(),
        title=parsed.title or resolved.stem,
        doc_type="paper",
        parsed=parsed.document,
        content_hash=content_hash,
        parser="pdf",
        parser_version=parsed.parser_version,
        max_chunk_chars=max_chunk_chars,
        source_id=source_id,
    )


def _resolve_pdf(path: Path, library_root: Path, max_bytes: int) -> tuple[Path, Path]:
    root = library_root.expanduser().resolve()
    candidate_path = path if path.is_absolute() else root / path
    resolved = candidate_path.expanduser().resolve()
    if not resolved.is_relative_to(root):
        raise LibraryPathError("PDF 路径必须位于资料目录内")
    if resolved.suffix.lower() != ".pdf":
        raise LibraryPathError("只允许导入 .pdf 文件")
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    size = resolved.stat().st_size
    if size > max_bytes:
        raise ValueError(f"PDF 大小 {size} bytes 超过上限 {max_bytes} bytes")
    return root, resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
