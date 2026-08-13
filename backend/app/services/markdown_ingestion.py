import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.ingest.chunking import chunk_by_heading
from app.ingest.markdown import parse_markdown
from app.llm.gateway import ModelGateway
from app.services.document_versions import activate_document_version, create_candidate_version


class LibraryPathError(ValueError):
    pass


@dataclass(frozen=True)
class IngestionResult:
    document_id: UUID
    version_id: UUID
    version_no: int
    block_count: int
    chunk_count: int
    activated: bool
    unchanged: bool


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
) -> IngestionResult:
    loaded = _read_markdown(path=path, library_root=library_root)
    return await _persist_markdown(
        session,
        gateway,
        loaded=loaded,
        max_chunk_chars=max_chunk_chars,
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


async def _persist_markdown(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    loaded: LoadedMarkdown,
    max_chunk_chars: int,
) -> IngestionResult:
    parsed = parse_markdown(loaded.content)
    chunks = chunk_by_heading(parsed, max_chars=max_chunk_chars)
    title = _document_title(parsed.blocks[0].text, loaded.fallback_title)
    content_hash = hashlib.sha256(parsed.full_text.encode()).hexdigest()

    source_id, document_id = await _upsert_document(
        session,
        library_root=loaded.root,
        source_uri=loaded.source_uri,
        title=title,
    )
    del source_id
    candidate = await create_candidate_version(
        session,
        document_id=document_id,
        content_hash=content_hash,
        parser="markdown",
        parser_version="1",
    )
    if not candidate.created:
        current = (
            (
                await session.execute(
                    text(
                        """
                        SELECT activated_at,
                               (SELECT count(*) FROM parsed_blocks WHERE version_id=:id) blocks,
                               (SELECT count(*) FROM chunks WHERE version_id=:id) chunks
                        FROM document_versions WHERE id=:id
                        """
                    ),
                    {"id": candidate.id},
                )
            )
            .mappings()
            .one()
        )
        await session.rollback()
        return IngestionResult(
            document_id=document_id,
            version_id=candidate.id,
            version_no=candidate.version_no,
            block_count=current["blocks"],
            chunk_count=current["chunks"],
            activated=current["activated_at"] is not None,
            unchanged=True,
        )

    try:
        embedding_result = await gateway.embed(
            [chunk.content for chunk in chunks], task_type="document_embedding"
        )
        # 审计记录先独立落盘, 后续解析事务失败也不能抹掉实际模型调用。
        await session.commit()
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE document_versions
                    SET parse_status='parsing', full_text=:full_text
                    WHERE id=:version_id
                    """
                ),
                {"version_id": candidate.id, "full_text": parsed.full_text},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO parsed_blocks
                        (id, version_id, block_idx, block_type, text, char_start, char_end,
                         heading_path)
                    VALUES
                        (:id, :version_id, :block_idx, :block_type, :text, :char_start,
                         :char_end, :heading_path)
                    """
                ),
                [
                    {
                        "id": uuid7(),
                        "version_id": candidate.id,
                        "block_idx": block.block_idx,
                        "block_type": block.block_type,
                        "text": block.text,
                        "char_start": block.char_start,
                        "char_end": block.char_end,
                        "heading_path": list(block.heading_path) or None,
                    }
                    for block in parsed.blocks
                ],
            )
            for chunk, embedding in zip(chunks, embedding_result.embeddings, strict=True):
                await session.execute(
                    text(
                        """
                        INSERT INTO chunks
                            (id, version_id, strategy, chunk_index, content, content_tokens,
                             block_start_idx, block_end_idx, char_start, char_end,
                             dominant_block_type, heading_path, embedding, doc_type)
                        VALUES
                            (:id, :version_id, 'heading', :chunk_index, :content,
                             :content_tokens, :block_start_idx, :block_end_idx, :char_start,
                             :char_end, :dominant_block_type, :heading_path,
                             CAST(:embedding AS vector), 'note')
                        """
                    ),
                    {
                        "id": uuid7(),
                        "version_id": candidate.id,
                        "chunk_index": chunk.chunk_index,
                        "content": chunk.content,
                        "content_tokens": chunk.content_tokens,
                        "block_start_idx": chunk.block_start_idx,
                        "block_end_idx": chunk.block_end_idx,
                        "char_start": chunk.char_start,
                        "char_end": chunk.char_end,
                        "dominant_block_type": chunk.dominant_block_type,
                        "heading_path": list(chunk.heading_path) or None,
                        "embedding": _vector_literal(embedding),
                    },
                )
            await session.execute(
                text("UPDATE document_versions SET parse_status='done' WHERE id=:version_id"),
                {"version_id": candidate.id},
            )
    except Exception as error:
        try:
            await session.execute(
                text(
                    """
                    UPDATE document_versions
                    SET parse_status='failed', parse_error=:error
                    WHERE id=:version_id
                    """
                ),
                {"version_id": candidate.id, "error": str(error)[:4000]},
            )
            await session.commit()
        except Exception:
            await session.rollback()
            async with session.begin():
                await session.execute(
                    text(
                        """
                        UPDATE document_versions
                        SET parse_status='failed', parse_error=:error
                        WHERE id=:version_id
                        """
                    ),
                    {"version_id": candidate.id, "error": str(error)[:4000]},
                )
        raise

    activated = await activate_document_version(session, candidate.id)
    return IngestionResult(
        document_id=document_id,
        version_id=candidate.id,
        version_no=candidate.version_no,
        block_count=len(parsed.blocks),
        chunk_count=len(chunks),
        activated=activated,
        unchanged=False,
    )


async def _upsert_document(
    session: AsyncSession,
    *,
    library_root: Path,
    source_uri: str,
    title: str,
) -> tuple[UUID, UUID]:
    root_json = json.dumps({"root": str(library_root)})
    async with session.begin():
        source_id = (
            await session.execute(
                text(
                    """
                    SELECT id FROM sources
                    WHERE kind='local_dir' AND config->>'root'=:root
                    ORDER BY created_at LIMIT 1
                    """
                ),
                {"root": str(library_root)},
            )
        ).scalar_one_or_none()
        if source_id is None:
            source_id = uuid7()
            await session.execute(
                text(
                    """
                    INSERT INTO sources (id, kind, name, config)
                    VALUES (:id, 'local_dir', :name, CAST(:config AS jsonb))
                    """
                ),
                {"id": source_id, "name": library_root.name, "config": root_json},
            )

        document_id = (
            await session.execute(
                text("SELECT id FROM documents WHERE source_id=:source_id AND source_uri=:uri"),
                {"source_id": source_id, "uri": source_uri},
            )
        ).scalar_one_or_none()
        if document_id is None:
            document_id = uuid7()
            await session.execute(
                text(
                    """
                    INSERT INTO documents
                        (id, source_id, source_uri, title, doc_type)
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
        else:
            await session.execute(
                text("UPDATE documents SET title=:title, deleted_at=NULL WHERE id=:id"),
                {"id": document_id, "title": title},
            )
    return source_id, document_id


def _document_title(first_block: str, fallback: str) -> str:
    if first_block.lstrip().startswith("#"):
        return first_block.lstrip("# ").strip() or fallback
    return fallback


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(format(value, ".9g") for value in vector) + "]"
