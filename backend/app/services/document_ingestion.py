import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.ingest.chunking import chunk_by_heading
from app.ingest.types import ParsedDocument
from app.llm.gateway import ModelGateway
from app.services.document_versions import activate_document_version, create_candidate_version


@dataclass(frozen=True)
class IngestionResult:
    document_id: UUID
    version_id: UUID
    version_no: int
    block_count: int
    chunk_count: int
    activated: bool
    unchanged: bool
    content_hash: str


async def persist_parsed_document(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    library_root: Path,
    source_uri: str,
    title: str,
    doc_type: str,
    parsed: ParsedDocument,
    content_hash: str,
    parser: str,
    parser_version: str,
    max_chunk_chars: int,
    source_id: UUID | None = None,
) -> IngestionResult:
    chunks = chunk_by_heading(parsed, max_chars=max_chunk_chars)
    _, document_id = await _upsert_document(
        session,
        library_root=library_root,
        source_uri=source_uri,
        title=title,
        doc_type=doc_type,
        source_id=source_id,
    )
    candidate = await create_candidate_version(
        session,
        document_id=document_id,
        content_hash=content_hash,
        parser=parser,
        parser_version=parser_version,
        embedding_model=gateway.embedding_model,
        embedding_provider=gateway.embedding_provider,
        embedding_revision=gateway.embedding_revision,
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
        if current["activated_at"] is not None:
            async with session.begin():
                await session.execute(
                    text("UPDATE documents SET deleted_at=NULL WHERE id=:document_id"),
                    {"document_id": document_id},
                )
                await session.execute(
                    text("UPDATE chunks SET is_searchable=true WHERE version_id=:version_id"),
                    {"version_id": candidate.id},
                )
        return IngestionResult(
            document_id=document_id,
            version_id=candidate.id,
            version_no=candidate.version_no,
            block_count=current["blocks"],
            chunk_count=current["chunks"],
            activated=current["activated_at"] is not None,
            unchanged=True,
            content_hash=content_hash,
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
                    SET parse_status='parsing', full_text=:full_text, page_count=:page_count
                    WHERE id=:version_id
                    """
                ),
                {
                    "version_id": candidate.id,
                    "full_text": parsed.full_text,
                    "page_count": parsed.page_count,
                },
            )
            block_ids = [uuid7() for _ in parsed.blocks]
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
                        "id": block_id,
                        "version_id": candidate.id,
                        "block_idx": block.block_idx,
                        "block_type": block.block_type,
                        "text": block.text,
                        "char_start": block.char_start,
                        "char_end": block.char_end,
                        "heading_path": list(block.heading_path) or None,
                    }
                    for block_id, block in zip(block_ids, parsed.blocks, strict=True)
                ],
            )
            locations = [
                {
                    "block_id": block_id,
                    "location_idx": location_idx,
                    "page_no": location.page_no,
                    "page_width": location.page_width,
                    "page_height": location.page_height,
                    "rotation": location.rotation,
                    "coord_origin": location.coord_origin,
                    "bbox_norm": json.dumps(location.bbox_norm),
                }
                for block_id, block in zip(block_ids, parsed.blocks, strict=True)
                for location_idx, location in enumerate(block.locations)
            ]
            if locations:
                await session.execute(
                    text(
                        """
                        INSERT INTO parsed_block_locations
                            (block_id, location_idx, page_no, page_width, page_height,
                             rotation, coord_origin, bbox_norm)
                        VALUES
                            (:block_id, :location_idx, :page_no, :page_width, :page_height,
                             :rotation, :coord_origin, CAST(:bbox_norm AS jsonb))
                        """
                    ),
                    locations,
                )
            for chunk, embedding in zip(chunks, embedding_result.embeddings, strict=True):
                await session.execute(
                    text(
                        """
                        INSERT INTO chunks
                            (id, version_id, strategy, chunk_index, content, content_tokens,
                             block_start_idx, block_end_idx, char_start, char_end,
                             dominant_block_type, heading_path, embedding, doc_type,
                             embedding_model, embedding_provider, embedding_revision)
                        VALUES
                            (:id, :version_id, 'heading', :chunk_index, :content,
                             :content_tokens, :block_start_idx, :block_end_idx, :char_start,
                             :char_end, :dominant_block_type, :heading_path,
                             CAST(:embedding AS vector), :doc_type, :embedding_model,
                             :embedding_provider, :embedding_revision)
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
                        "doc_type": doc_type,
                        "embedding_model": gateway.embedding_model,
                        "embedding_provider": gateway.embedding_provider,
                        "embedding_revision": gateway.embedding_revision,
                    },
                )
            await session.execute(
                text("UPDATE document_versions SET parse_status='done' WHERE id=:version_id"),
                {"version_id": candidate.id},
            )
    except Exception as error:
        await _mark_version_failed(session, candidate.id, error)
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
        content_hash=content_hash,
    )


async def _upsert_document(
    session: AsyncSession,
    *,
    library_root: Path,
    source_uri: str,
    title: str,
    doc_type: str,
    source_id: UUID | None,
) -> tuple[UUID, UUID]:
    root_json = json.dumps({"root": str(library_root)})
    async with session.begin():
        if source_id is None:
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
                    INSERT INTO documents (id, source_id, source_uri, title, doc_type)
                    VALUES (:id, :source_id, :source_uri, :title, :doc_type)
                    """
                ),
                {
                    "id": document_id,
                    "source_id": source_id,
                    "source_uri": source_uri,
                    "title": title,
                    "doc_type": doc_type,
                },
            )
        else:
            await session.execute(
                text("UPDATE documents SET title=:title, doc_type=:doc_type WHERE id=:id"),
                {"id": document_id, "title": title, "doc_type": doc_type},
            )
    return source_id, document_id


async def _mark_version_failed(session: AsyncSession, version_id: UUID, error: Exception) -> None:
    try:
        await session.execute(
            text(
                """
                UPDATE document_versions
                SET parse_status='failed', parse_error=:error
                WHERE id=:version_id
                """
            ),
            {"version_id": version_id, "error": str(error)[:4000]},
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
                {"version_id": version_id, "error": str(error)[:4000]},
            )


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(format(value, ".9g") for value in vector) + "]"
