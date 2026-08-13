import hashlib
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest.markdown import parse_markdown
from app.llm.gateway import ModelGateway
from app.services.document_ingestion import IngestionResult, persist_parsed_document


class LibraryPathError(ValueError):
    pass


@dataclass(frozen=True)
class LoadedMarkdown:
    root: Path
    source_uri: str
    fallback_title: str
    content: str


async def ingest_markdown_file(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    path: Path,
    library_root: Path,
    max_chunk_chars: int = 2000,
    source_id: UUID | None = None,
) -> IngestionResult:
    loaded = _read_markdown(path=path, library_root=library_root)
    parsed = parse_markdown(loaded.content)
    title = _document_title(parsed.blocks[0].text, loaded.fallback_title)
    content_hash = hashlib.sha256(parsed.full_text.encode()).hexdigest()
    return await persist_parsed_document(
        session,
        gateway,
        library_root=loaded.root,
        source_uri=loaded.source_uri,
        title=title,
        doc_type="note",
        parsed=parsed,
        content_hash=content_hash,
        parser="markdown",
        parser_version="1",
        max_chunk_chars=max_chunk_chars,
        source_id=source_id,
    )


def _read_markdown(*, path: Path, library_root: Path) -> LoadedMarkdown:
    root = library_root.expanduser().resolve()
    candidate_path = path if path.is_absolute() else root / path
    resolved = candidate_path.expanduser().resolve()
    if not resolved.is_relative_to(root):
        raise LibraryPathError("Markdown 路径必须位于 LOCAL_LIBRARY_PATH 内")
    if resolved.suffix.lower() not in {".md", ".markdown"}:
        raise LibraryPathError("只允许导入 .md 或 .markdown 文件")
    if not resolved.is_file():
        raise FileNotFoundError(resolved)

    return LoadedMarkdown(
        root=root,
        source_uri=resolved.relative_to(root).as_posix(),
        fallback_title=resolved.stem,
        content=resolved.read_text(encoding="utf-8"),
    )


def _document_title(first_block: str, fallback: str) -> str:
    if first_block.lstrip().startswith("#"):
        return first_block.lstrip("# ").strip() or fallback
    return fallback
