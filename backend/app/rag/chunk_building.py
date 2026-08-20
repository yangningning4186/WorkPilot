from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid5

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest.chunk_strategies import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    DEFAULT_SEMANTIC_MIN_TOKENS,
    HARD_MAX_TOKENS,
    StrategyChunk,
    chunk_fixed,
    chunk_recursive,
    chunk_semantic,
    count_tokens,
    semantic_units,
)
from app.ingest.types import ParsedBlock, ParsedDocument
from workpilot_ai.gateway import ModelGateway

ChunkStrategy = Literal["fixed", "recursive", "semantic"]
CHUNK_STRATEGIES: tuple[ChunkStrategy, ...] = ("fixed", "recursive", "semantic")
_CHUNK_UUID_NAMESPACE = UUID("32698a4d-d32d-5a5c-908f-86f3d3d70e75")
_ALGORITHM_VERSIONS: dict[ChunkStrategy, str] = {
    "fixed": "unicode-fixed:1",
    "recursive": "paragraph-line-sentence:1",
    "semantic": "adjacent-cosine-mad:1",
}


class ChunkBuildVersionError(ValueError):
    pass


@dataclass(frozen=True)
class StrategyBuildResult:
    strategy: ChunkStrategy
    chunk_count: int
    rebuilt: bool
    build_signature: str


@dataclass(frozen=True)
class VersionChunkBuildResult:
    version_id: UUID
    strategies: tuple[StrategyBuildResult, ...]


@dataclass(frozen=True)
class _VersionSource:
    version_id: UUID
    title: str
    doc_type: str
    parsed: ParsedDocument
    searchable: bool
    embedding_model: str
    embedding_provider: str
    embedding_revision: str


async def build_chunk_strategies(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    version_id: UUID,
    strategies: Sequence[ChunkStrategy] = CHUNK_STRATEGIES,
    embedding_batch_size: int = 32,
) -> VersionChunkBuildResult:
    """Build selected offline strategies without touching the existing heading rows.

    A strategy with a matching build signature and valid stored spans is a no-op. Rebuilds
    use deterministic UUIDs and replace only that strategy inside one version-row lock.
    """

    selected = _normalize_strategies(strategies)
    if embedding_batch_size < 1:
        raise ValueError("embedding_batch_size 必须大于 0")
    source = await _load_version_source(session, version_id)
    _validate_embedding_identity(source, gateway)
    signatures = {strategy: _build_signature(strategy, source=source) for strategy in selected}
    existing = {
        strategy: await _load_existing_chunks(session, source, strategy, signatures[strategy])
        for strategy in selected
    }
    await session.rollback()

    changed = [strategy for strategy in selected if existing[strategy] is None]
    if not changed:
        return VersionChunkBuildResult(
            version_id=version_id,
            strategies=tuple(
                StrategyBuildResult(
                    strategy=strategy,
                    chunk_count=existing[strategy] or 0,
                    rebuilt=False,
                    build_signature=signatures[strategy],
                )
                for strategy in selected
            ),
        )

    planned: dict[ChunkStrategy, list[StrategyChunk]] = {}
    if "fixed" in changed:
        planned["fixed"] = chunk_fixed(source.parsed)
    if "recursive" in changed:
        planned["recursive"] = chunk_recursive(source.parsed)
    if "semantic" in changed:
        units = semantic_units(source.parsed)
        unit_embeddings = await _embed_texts(
            gateway,
            [source.parsed.full_text[unit.char_start : unit.char_end] for unit in units],
            batch_size=embedding_batch_size,
            task_type="semantic_chunk_boundary_embedding",
        )
        planned["semantic"] = chunk_semantic(source.parsed, units, unit_embeddings)

    chunk_embeddings: dict[ChunkStrategy, list[list[float]]] = {}
    for strategy in changed:
        chunks = planned[strategy]
        if not chunks:
            raise ChunkBuildVersionError(f"{strategy} 未生成任何 chunk")
        chunk_embeddings[strategy] = await _embed_texts(
            gateway,
            [chunk.content for chunk in chunks],
            batch_size=embedding_batch_size,
            task_type=f"{strategy}_chunk_embedding",
        )
    # Gateway 的审计和费用记录先持久化; chunk 替换随后在独立事务中原子完成。
    await session.commit()

    rebuilt: set[ChunkStrategy] = set()
    async with session.begin():
        locked = (
            (
                await session.execute(
                    text(
                        """
                        SELECT full_text, embedding_model, embedding_provider, embedding_revision
                        FROM document_versions WHERE id=:version_id FOR UPDATE
                        """
                    ),
                    {"version_id": version_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if locked is None or locked["full_text"] != source.parsed.full_text:
            raise ChunkBuildVersionError("构建期间 document version 已变化")
        if (
            locked["embedding_model"],
            locked["embedding_provider"],
            locked["embedding_revision"],
        ) != (
            source.embedding_model,
            source.embedding_provider,
            source.embedding_revision,
        ):
            raise ChunkBuildVersionError("构建期间 embedding 身份已变化")

        for strategy in changed:
            # 并发构建者可能已经写完同一签名; 锁内再检查一次, 避免重复替换。
            current_count = await _matching_signature_count(
                session, version_id, strategy, signatures[strategy]
            )
            if current_count is not None:
                existing[strategy] = current_count
                continue
            await session.execute(
                text("DELETE FROM chunks WHERE version_id=:version_id AND strategy=:strategy"),
                {"version_id": version_id, "strategy": strategy},
            )
            await _insert_strategy_chunks(
                session,
                source=source,
                strategy=strategy,
                signature=signatures[strategy],
                chunks=planned[strategy],
                embeddings=chunk_embeddings[strategy],
            )
            existing[strategy] = len(planned[strategy])
            rebuilt.add(strategy)

    return VersionChunkBuildResult(
        version_id=version_id,
        strategies=tuple(
            StrategyBuildResult(
                strategy=strategy,
                chunk_count=existing[strategy] or 0,
                rebuilt=strategy in rebuilt,
                build_signature=signatures[strategy],
            )
            for strategy in selected
        ),
    )


async def list_active_version_ids(session: AsyncSession) -> list[UUID]:
    version_ids = list(
        (
            await session.execute(
                text(
                    """
                    SELECT v.id
                    FROM document_versions v
                    JOIN documents d ON d.id=v.document_id
                    WHERE v.activated_at IS NOT NULL
                      AND v.invalid_at IS NULL
                      AND d.deleted_at IS NULL
                    ORDER BY d.source_uri, v.id
                    """
                )
            )
        ).scalars()
    )
    await session.rollback()
    return version_ids


async def _load_version_source(session: AsyncSession, version_id: UUID) -> _VersionSource:
    version = (
        (
            await session.execute(
                text(
                    """
                    SELECT v.id, v.full_text, v.parse_status,
                           v.embedding_model, v.embedding_provider, v.embedding_revision,
                           d.title, d.doc_type,
                           (v.activated_at IS NOT NULL AND v.invalid_at IS NULL
                            AND d.deleted_at IS NULL) AS searchable
                    FROM document_versions v
                    JOIN documents d ON d.id=v.document_id
                    WHERE v.id=:version_id
                    """
                ),
                {"version_id": version_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if version is None:
        raise ChunkBuildVersionError(f"document version 不存在: {version_id}")
    if version["parse_status"] != "done" or not isinstance(version["full_text"], str):
        raise ChunkBuildVersionError("document version 尚未完成解析")
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT block_idx, block_type, text, char_start, char_end,
                           COALESCE(heading_path, ARRAY[]::text[]) AS heading_path
                    FROM parsed_blocks
                    WHERE version_id=:version_id
                    ORDER BY block_idx
                    """
                ),
                {"version_id": version_id},
            )
        )
        .mappings()
        .all()
    )
    full_text = version["full_text"]
    blocks = [
        ParsedBlock(
            block_idx=row["block_idx"],
            block_type=row["block_type"],
            text=row["text"],
            char_start=row["char_start"],
            char_end=row["char_end"],
            heading_path=tuple(row["heading_path"]),
        )
        for row in rows
    ]
    if not blocks:
        raise ChunkBuildVersionError("document version 没有 parsed block")
    for expected, block in enumerate(blocks):
        if block.block_idx != expected:
            raise ChunkBuildVersionError("parsed block_idx 不连续")
        if full_text[block.char_start : block.char_end] != block.text:
            raise ChunkBuildVersionError(f"parsed block {block.block_idx} 字符区间与原文不一致")
    return _VersionSource(
        version_id=version_id,
        title=version["title"],
        doc_type=version["doc_type"],
        parsed=ParsedDocument(full_text=full_text, blocks=blocks),
        searchable=bool(version["searchable"]),
        embedding_model=version["embedding_model"],
        embedding_provider=version["embedding_provider"],
        embedding_revision=version["embedding_revision"],
    )


async def _load_existing_chunks(
    session: AsyncSession,
    source: _VersionSource,
    strategy: ChunkStrategy,
    signature: str,
) -> int | None:
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT chunk_index, content, content_tokens, block_start_idx, block_end_idx,
                           char_start, char_end, build_signature, embedding IS NOT NULL AS embedded,
                           embedding_model, embedding_provider, embedding_revision, is_searchable
                    FROM chunks
                    WHERE version_id=:version_id AND strategy=:strategy
                    ORDER BY chunk_index
                    """
                ),
                {"version_id": source.version_id, "strategy": strategy},
            )
        )
        .mappings()
        .all()
    )
    if not rows:
        return None
    for expected, row in enumerate(rows):
        start = int(row["char_start"])
        end = int(row["char_end"])
        intersecting = [
            block
            for block in source.parsed.blocks
            if block.char_start < end and block.char_end > start
        ]
        if (
            row["chunk_index"] != expected
            or row["build_signature"] != signature
            or not row["embedded"]
            or not 0 <= start < end <= len(source.parsed.full_text)
            or row["content"] != source.parsed.full_text[start:end]
            or row["content_tokens"] != count_tokens(row["content"])
            or not intersecting
            or row["block_start_idx"] != intersecting[0].block_idx
            or row["block_end_idx"] != intersecting[-1].block_idx
            or bool(row["is_searchable"]) != source.searchable
            or (
                row["embedding_model"],
                row["embedding_provider"],
                row["embedding_revision"],
            )
            != (
                source.embedding_model,
                source.embedding_provider,
                source.embedding_revision,
            )
        ):
            return None
    return len(rows)


async def _matching_signature_count(
    session: AsyncSession,
    version_id: UUID,
    strategy: ChunkStrategy,
    signature: str,
) -> int | None:
    row = (
        (
            await session.execute(
                text(
                    """
                    SELECT count(*) AS total,
                           count(*) FILTER (WHERE build_signature=:signature
                                             AND embedding IS NOT NULL) AS matching,
                           min(chunk_index) AS first_index,
                           max(chunk_index) AS last_index
                    FROM chunks WHERE version_id=:version_id AND strategy=:strategy
                    """
                ),
                {"version_id": version_id, "strategy": strategy, "signature": signature},
            )
        )
        .mappings()
        .one()
    )
    total = int(row["total"])
    if (
        total > 0
        and row["matching"] == total
        and row["first_index"] == 0
        and row["last_index"] == total - 1
    ):
        return total
    return None


async def _insert_strategy_chunks(
    session: AsyncSession,
    *,
    source: _VersionSource,
    strategy: ChunkStrategy,
    signature: str,
    chunks: Sequence[StrategyChunk],
    embeddings: Sequence[Sequence[float]],
) -> None:
    if len(chunks) != len(embeddings):
        raise ChunkBuildVersionError("chunk 与 embedding 数量不一致")
    statement = text(
        """
        INSERT INTO chunks
            (id, version_id, strategy, chunk_index, content, content_tokens,
             block_start_idx, block_end_idx, char_start, char_end,
             dominant_block_type, heading_path, embedding, doc_type, is_searchable,
             embedding_model, embedding_provider, embedding_revision, build_signature,
             tsv_en, tsv_zh)
        VALUES
            (:id, :version_id, :strategy, :chunk_index, :content, :content_tokens,
             :block_start_idx, :block_end_idx, :char_start, :char_end,
             :dominant_block_type, :heading_path, CAST(:embedding AS vector), :doc_type,
             :is_searchable, :embedding_model, :embedding_provider, :embedding_revision,
             :build_signature,
             setweight(to_tsvector('english', lexical_en_text(:title)), 'A') ||
             setweight(to_tsvector('english', lexical_en_text(:heading_text)), 'B') ||
             setweight(to_tsvector('english', lexical_en_text(:content)), 'D'),
             setweight(to_tsvector('simple', lexical_zh_bigrams(:title)), 'A') ||
             setweight(to_tsvector('simple', lexical_zh_bigrams(:heading_text)), 'B') ||
             setweight(to_tsvector('simple', lexical_zh_bigrams(:content)), 'D'))
        """
    )
    await session.execute(
        statement,
        [
            {
                "id": _chunk_id(source.version_id, strategy, signature, chunk),
                "version_id": source.version_id,
                "strategy": strategy,
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
                "doc_type": source.doc_type,
                "is_searchable": source.searchable,
                "embedding_model": source.embedding_model,
                "embedding_provider": source.embedding_provider,
                "embedding_revision": source.embedding_revision,
                "build_signature": signature,
                "title": source.title,
                "heading_text": " ".join(chunk.heading_path),
            }
            for chunk, embedding in zip(chunks, embeddings, strict=True)
        ],
    )


async def _embed_texts(
    gateway: ModelGateway,
    values: Sequence[str],
    *,
    batch_size: int,
    task_type: str,
) -> list[list[float]]:
    embeddings: list[list[float]] = []
    for start in range(0, len(values), batch_size):
        result = await gateway.embed(list(values[start : start + batch_size]), task_type=task_type)
        embeddings.extend(result.embeddings)
    return embeddings


def _build_signature(strategy: ChunkStrategy, *, source: _VersionSource) -> str:
    parameters: dict[str, object] = {
        "strategy": strategy,
        "algorithm": _ALGORITHM_VERSIONS[strategy],
        "tokenizer": "unicode-codepoint-word:1",
        "max_tokens": DEFAULT_MAX_TOKENS,
        "hard_max_tokens": HARD_MAX_TOKENS,
        "embedding_model": source.embedding_model,
        "embedding_provider": source.embedding_provider,
        "embedding_revision": source.embedding_revision,
    }
    if strategy == "fixed":
        parameters["overlap_tokens"] = DEFAULT_OVERLAP_TOKENS
    if strategy == "semantic":
        parameters["min_tokens"] = DEFAULT_SEMANTIC_MIN_TOKENS
    encoded = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _chunk_id(
    version_id: UUID,
    strategy: ChunkStrategy,
    signature: str,
    chunk: StrategyChunk,
) -> UUID:
    identity = (
        f"{version_id}:{strategy}:{signature}:{chunk.chunk_index}:"
        f"{chunk.char_start}:{chunk.char_end}"
    )
    return uuid5(_CHUNK_UUID_NAMESPACE, identity)


def _normalize_strategies(strategies: Iterable[ChunkStrategy]) -> tuple[ChunkStrategy, ...]:
    selected: list[ChunkStrategy] = []
    for strategy in strategies:
        if strategy not in CHUNK_STRATEGIES:
            raise ValueError(f"未知 chunk strategy: {strategy}")
        if strategy not in selected:
            selected.append(strategy)
    if not selected:
        raise ValueError("至少选择一个 chunk strategy")
    return tuple(selected)


def _validate_embedding_identity(source: _VersionSource, gateway: ModelGateway) -> None:
    expected = (
        source.embedding_model,
        source.embedding_provider,
        source.embedding_revision,
    )
    actual = (
        gateway.embedding_model,
        gateway.embedding_provider,
        gateway.embedding_revision,
    )
    if actual != expected:
        raise ChunkBuildVersionError(
            "离线构建必须复用 document version 的 embedding 身份: "
            f"version={expected}, gateway={actual}"
        )


def _vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(format(value, ".9g") for value in vector) + "]"
