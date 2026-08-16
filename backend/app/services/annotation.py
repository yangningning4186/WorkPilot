import asyncio
import json
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import pymupdf
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.schemas.annotation import (
    AnnotationBlockPageResponse,
    AnnotationBlockResponse,
    AnnotationDatasetCreate,
    AnnotationDatasetResponse,
    AnnotationDocumentResponse,
    AnnotationItemListResponse,
    AnnotationItemResponse,
    AnnotationItemUpsert,
    AnnotationLocationResponse,
    GoldSpanInput,
    GoldSpanResponse,
    GoldToolInput,
    ResolveSpanRequest,
)


class AnnotationNotFoundError(LookupError):
    pass


class AnnotationConflictError(ValueError):
    pass


async def list_datasets(session: AsyncSession) -> list[AnnotationDatasetResponse]:
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT ds.id, ds.name, ds.split, ds.version, ds.description,
                           count(i.id) AS item_count,
                           count(i.id) FILTER (WHERE
                               CASE i.category
                                 WHEN 'unanswerable' THEN
                                   jsonb_array_length(i.gold_spans)=0
                                   AND jsonb_array_length(i.gold_tools)=0
                                   AND COALESCE(btrim(i.gold_answer), '')=''
                                 WHEN 'global' THEN
                                   COALESCE(btrim(i.gold_answer), '')<>''
                                   AND jsonb_array_length(i.gold_tools)=0
                                   AND (jsonb_array_length(i.gold_spans)=0
                                        OR validate_eval_spans(i.gold_spans))
                                 WHEN 'agent_task' THEN
                                   jsonb_array_length(i.gold_tools)>0
                                   AND (jsonb_array_length(i.gold_spans)=0
                                        OR validate_eval_spans(i.gold_spans))
                                 WHEN 'temporal' THEN
                                   jsonb_array_length(i.gold_spans)>0
                                   AND validate_eval_spans(i.gold_spans)
                                   AND COALESCE(btrim(i.gold_answer), '')<>''
                                   AND i.temporal_ctx IS NOT NULL
                                   AND jsonb_array_length(i.gold_tools)=0
                                 ELSE
                                   jsonb_array_length(i.gold_spans)>0
                                   AND validate_eval_spans(i.gold_spans)
                                   AND COALESCE(btrim(i.gold_answer), '')<>''
                                   AND jsonb_array_length(i.gold_tools)=0
                               END
                           ) AS valid_count,
                           count(i.id) FILTER (
                               WHERE jsonb_array_length(i.gold_spans) > 0
                                 AND NOT validate_eval_spans(i.gold_spans)
                           ) AS stale_count
                    FROM eval_datasets ds
                    LEFT JOIN eval_items i ON i.dataset_id=ds.id
                    GROUP BY ds.id
                    ORDER BY ds.name
                    """
                )
            )
        )
        .mappings()
        .all()
    )
    await session.rollback()
    return [AnnotationDatasetResponse(**row) for row in rows]


async def create_dataset(
    session: AsyncSession, request: AnnotationDatasetCreate
) -> AnnotationDatasetResponse:
    async with session.begin():
        row = (
            (
                await session.execute(
                    text(
                        """
                        INSERT INTO eval_datasets (id, name, split, version, description)
                        VALUES (:id, :name, :split, :version, :description)
                        ON CONFLICT (name) DO UPDATE SET
                            split=EXCLUDED.split,
                            version=EXCLUDED.version,
                            description=EXCLUDED.description
                        RETURNING id, name, split, version, description
                        """
                    ),
                    {"id": uuid7(), **request.model_dump()},
                )
            )
            .mappings()
            .one()
        )
    return AnnotationDatasetResponse(**row, item_count=0, valid_count=0, stale_count=0)


async def list_documents(
    session: AsyncSession, *, query: str = ""
) -> list[AnnotationDocumentResponse]:
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT d.id AS document_id, v.id AS version_id, d.title, d.source_uri,
                           v.parser, v.parser_version, v.page_count, s.kind AS source_kind,
                           count(b.id) AS block_count
                    FROM documents d
                    JOIN sources s ON s.id=d.source_id
                    JOIN document_versions v ON v.document_id=d.id
                      AND v.activated_at IS NOT NULL AND v.invalid_at IS NULL
                    JOIN parsed_blocks b ON b.version_id=v.id
                    WHERE d.deleted_at IS NULL
                      AND (:query='' OR d.title ILIKE '%' || :query || '%'
                           OR d.source_uri ILIKE '%' || :query || '%')
                    GROUP BY d.id, v.id, s.kind
                    ORDER BY d.title, d.source_uri
                    LIMIT 200
                    """
                ),
                {"query": query.strip()},
            )
        )
        .mappings()
        .all()
    )
    await session.rollback()
    return [AnnotationDocumentResponse(**row) for row in rows]


async def list_blocks(
    session: AsyncSession,
    version_id: UUID,
    *,
    offset: int,
    limit: int,
    query: str,
    block_type: str,
) -> AnnotationBlockPageResponse:
    filters = """
        b.version_id=:version_id
        AND (:query='' OR b.text ILIKE '%' || :query || '%')
        AND (:block_type='' OR b.block_type=:block_type)
    """
    parameters = {
        "version_id": version_id,
        "query": query.strip(),
        "block_type": block_type.strip(),
        "offset": offset,
        "limit": limit,
    }
    exists = (
        await session.execute(
            text("SELECT 1 FROM document_versions WHERE id=:version_id"),
            {"version_id": version_id},
        )
    ).scalar_one_or_none()
    if exists is None:
        await session.rollback()
        raise AnnotationNotFoundError(str(version_id))
    total = (
        await session.execute(
            text(f"SELECT count(*) FROM parsed_blocks b WHERE {filters}"), parameters
        )
    ).scalar_one()
    rows = (
        (
            await session.execute(
                text(
                    f"""
                    SELECT b.id AS block_id, b.version_id, b.block_idx, b.block_type,
                           b.text, b.char_start, b.char_end,
                           COALESCE(b.heading_path, ARRAY[]::text[]) AS heading_path,
                           COALESCE(
                               (SELECT jsonb_agg(jsonb_build_object(
                                   'page_no', l.page_no,
                                   'page_width', l.page_width,
                                   'page_height', l.page_height,
                                   'rotation', l.rotation,
                                   'coord_origin', l.coord_origin,
                                   'bbox_norm', l.bbox_norm
                               ) ORDER BY l.location_idx)
                                FROM parsed_block_locations l WHERE l.block_id=b.id),
                               '[]'::jsonb
                           ) AS locations
                    FROM parsed_blocks b
                    WHERE {filters}
                    ORDER BY b.block_idx
                    OFFSET :offset LIMIT :limit
                    """
                ),
                parameters,
            )
        )
        .mappings()
        .all()
    )
    await session.rollback()
    return AnnotationBlockPageResponse(
        items=[AnnotationBlockResponse(**row) for row in rows],
        total=total,
        offset=offset,
        limit=limit,
    )


async def resolve_span(session: AsyncSession, request: ResolveSpanRequest) -> GoldSpanResponse:
    row = (
        (
            await session.execute(
                text(
                    """
                    SELECT b.id AS block_id, b.version_id, b.text, b.char_start,
                           d.title, d.source_uri,
                           COALESCE(
                               (SELECT jsonb_agg(jsonb_build_object(
                                   'page_no', l.page_no,
                                   'page_width', l.page_width,
                                   'page_height', l.page_height,
                                   'rotation', l.rotation,
                                   'coord_origin', l.coord_origin,
                                   'bbox_norm', l.bbox_norm
                               ) ORDER BY l.location_idx)
                                FROM parsed_block_locations l WHERE l.block_id=b.id),
                               '[]'::jsonb
                           ) AS locations
                    FROM parsed_blocks b
                    JOIN document_versions v ON v.id=b.version_id
                    JOIN documents d ON d.id=v.document_id
                    WHERE b.id=:block_id
                    """
                ),
                {"block_id": request.block_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    await session.rollback()
    if row is None:
        raise AnnotationNotFoundError(str(request.block_id))
    local_start = utf16_offset_to_codepoint(row["text"], request.utf16_start)
    local_end = utf16_offset_to_codepoint(row["text"], request.utf16_end)
    actual_quote = row["text"][local_start:local_end]
    if actual_quote != request.quote:
        raise AnnotationConflictError("选区 quote 与 block 原文不一致, 请刷新页面后重新选择")
    return GoldSpanResponse(
        version_id=row["version_id"],
        block_id=row["block_id"],
        char_start=row["char_start"] + local_start,
        char_end=row["char_start"] + local_end,
        quote=actual_quote,
        note=request.note,
        title=row["title"],
        source_uri=row["source_uri"],
        locations=[AnnotationLocationResponse(**item) for item in row["locations"]],
    )


def utf16_offset_to_codepoint(value: str, offset: int) -> int:
    if offset < 0:
        raise AnnotationConflictError("UTF-16 offset 不能为负数")
    consumed = 0
    for index, character in enumerate(value):
        if consumed == offset:
            return index
        consumed += 2 if ord(character) > 0xFFFF else 1
        if consumed > offset:
            raise AnnotationConflictError("UTF-16 offset 落在代理对中间")
    if consumed == offset:
        return len(value)
    raise AnnotationConflictError("UTF-16 offset 超出 block 文本长度")


async def list_items(session: AsyncSession, *, dataset_id: UUID) -> AnnotationItemListResponse:
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT i.*,
                           validate_eval_spans(i.gold_spans) AS spans_valid
                    FROM eval_items i
                    WHERE i.dataset_id=:dataset_id
                    ORDER BY i.updated_at DESC, i.created_at DESC
                    """
                ),
                {"dataset_id": dataset_id},
            )
        )
        .mappings()
        .all()
    )
    await session.rollback()
    items = [_item_response(row) for row in rows]
    return AnnotationItemListResponse(items=items, total=len(items))


async def create_item(
    session: AsyncSession, request: AnnotationItemUpsert
) -> AnnotationItemResponse:
    spans = await validate_gold_spans(session, request.gold_spans)
    item_id = uuid7()
    await _write_item(session, item_id, request, spans, create=True)
    return await get_item(session, item_id)


async def update_item(
    session: AsyncSession, item_id: UUID, request: AnnotationItemUpsert
) -> AnnotationItemResponse:
    spans = await validate_gold_spans(session, request.gold_spans)
    await _write_item(session, item_id, request, spans, create=False)
    return await get_item(session, item_id)


async def delete_item(session: AsyncSession, item_id: UUID) -> None:
    async with session.begin():
        deleted_id = (
            await session.execute(
                text("DELETE FROM eval_items WHERE id=:id RETURNING id"), {"id": item_id}
            )
        ).scalar_one_or_none()
        if deleted_id is None:
            raise AnnotationNotFoundError(str(item_id))


async def get_item(session: AsyncSession, item_id: UUID) -> AnnotationItemResponse:
    row = (
        (
            await session.execute(
                text(
                    """
                    SELECT *, validate_eval_spans(gold_spans) AS spans_valid
                    FROM eval_items WHERE id=:id
                    """
                ),
                {"id": item_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    await session.rollback()
    if row is None:
        raise AnnotationNotFoundError(str(item_id))
    return _item_response(row)


async def validate_gold_spans(
    session: AsyncSession, spans: list[GoldSpanInput]
) -> list[dict[str, object]]:
    validated: list[dict[str, object]] = []
    for span in spans:
        full_text = (
            await session.execute(
                text("SELECT full_text FROM document_versions WHERE id=:id"),
                {"id": span.version_id},
            )
        ).scalar_one_or_none()
        if full_text is None:
            await session.rollback()
            raise AnnotationConflictError(f"版本不存在或没有全文: {span.version_id}")
        if span.char_end > len(full_text):
            await session.rollback()
            raise AnnotationConflictError(f"gold span 超过版本全文长度: {span.version_id}")
        if full_text[span.char_start : span.char_end] != span.quote:
            await session.rollback()
            raise AnnotationConflictError(f"gold span quote 无法从版本全文回切: {span.version_id}")
        validated.append(span.model_dump(mode="json"))
    await session.rollback()
    return validated


async def _write_item(
    session: AsyncSession,
    item_id: UUID,
    request: AnnotationItemUpsert,
    spans: list[dict[str, object]],
    *,
    create: bool,
) -> None:
    constraints = {
        "must_include": _clean_terms(request.must_include),
        "must_not_include": _clean_terms(request.must_not_include),
    }
    parameters = {
        "id": item_id,
        "dataset_id": request.dataset_id,
        "category": request.category,
        "question": request.question.strip(),
        "gold_answer": request.gold_answer.strip() if request.gold_answer else None,
        "gold_spans": json.dumps(spans, ensure_ascii=False),
        "gold_tools": json.dumps(
            [tool.model_dump(mode="json") for tool in request.gold_tools],
            ensure_ascii=False,
        ),
        "constraints": json.dumps(constraints, ensure_ascii=False),
        "temporal_ctx": request.temporal_ctx,
        "difficulty": request.difficulty,
        "origin": request.origin,
    }
    async with session.begin():
        dataset_exists = (
            await session.execute(
                text("SELECT 1 FROM eval_datasets WHERE id=:id"), {"id": request.dataset_id}
            )
        ).scalar_one_or_none()
        if dataset_exists is None:
            raise AnnotationNotFoundError(str(request.dataset_id))
        if create:
            await session.execute(
                text(
                    """
                    INSERT INTO eval_items
                        (id, dataset_id, category, question, gold_answer, gold_spans,
                         gold_tools, constraints, difficulty, origin, temporal_ctx)
                    VALUES
                        (:id, :dataset_id, :category, :question, :gold_answer,
                         CAST(:gold_spans AS jsonb), CAST(:gold_tools AS jsonb),
                         CAST(:constraints AS jsonb),
                         :difficulty, :origin, :temporal_ctx)
                    """
                ),
                parameters,
            )
        else:
            updated_id = (
                await session.execute(
                    text(
                        """
                    UPDATE eval_items SET
                        dataset_id=:dataset_id, category=:category, question=:question,
                        gold_answer=:gold_answer, gold_spans=CAST(:gold_spans AS jsonb),
                        gold_tools=CAST(:gold_tools AS jsonb),
                        constraints=CAST(:constraints AS jsonb), difficulty=:difficulty,
                        origin=:origin, temporal_ctx=:temporal_ctx
                    WHERE id=:id
                    RETURNING id
                    """
                    ),
                    parameters,
                )
            ).scalar_one_or_none()
            if updated_id is None:
                raise AnnotationNotFoundError(str(item_id))


def _item_response(row: Any) -> AnnotationItemResponse:
    spans = [GoldSpanInput.model_validate(item) for item in row["gold_spans"]]
    tools = [GoldToolInput.model_validate(item) for item in row["gold_tools"]]
    issues: list[str] = []
    spans_valid = bool(row["spans_valid"])
    category = row["category"]
    has_answer = bool((row["gold_answer"] or "").strip())
    if category == "unanswerable":
        if spans:
            issues.append("unexpected_gold_spans")
        if has_answer:
            issues.append("unexpected_gold_answer")
        if tools:
            issues.append("unexpected_gold_tools")
    elif category == "agent_task":
        if not tools:
            issues.append("missing_gold_tools")
    else:
        if tools:
            issues.append("unexpected_gold_tools")
        if category == "global":
            if not has_answer:
                issues.append("missing_gold_answer")
        else:
            if not spans:
                issues.append("missing_gold_spans")
            if not has_answer:
                issues.append("missing_gold_answer")
    if row["category"] == "temporal" and row["temporal_ctx"] is None:
        issues.append("missing_temporal_ctx")
    elif row["category"] != "temporal" and row["temporal_ctx"] is not None:
        issues.append("unexpected_temporal_ctx")
    if spans and not spans_valid:
        issues.append("stale_gold_spans")
    status: Literal["valid", "stale", "invalid"] = (
        "valid" if not issues else ("stale" if "stale_gold_spans" in issues else "invalid")
    )
    constraints = row["constraints"] or {}
    return AnnotationItemResponse(
        id=row["id"],
        dataset_id=row["dataset_id"],
        category=row["category"],
        question=row["question"],
        gold_answer=row["gold_answer"],
        gold_spans=spans,
        gold_tools=tools,
        must_include=list(constraints.get("must_include") or []),
        must_not_include=list(constraints.get("must_not_include") or []),
        difficulty=row["difficulty"],
        origin=row["origin"],
        temporal_ctx=row["temporal_ctx"],
        status=status,
        issues=issues,
        updated_at=row["updated_at"],
    )


def _clean_terms(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


async def resolve_source_file(session: AsyncSession, version_id: UUID) -> tuple[Path, str]:
    row = (
        (
            await session.execute(
                text(
                    """
                    SELECT s.config->>'root' AS root, d.source_uri
                    FROM document_versions v
                    JOIN documents d ON d.id=v.document_id
                    JOIN sources s ON s.id=d.source_id
                    WHERE v.id=:version_id AND s.kind='local_dir'
                    """
                ),
                {"version_id": version_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    await session.rollback()
    if row is None:
        raise AnnotationNotFoundError(str(version_id))
    root, path, path_exists = await asyncio.to_thread(
        _resolve_local_path, row["root"], row["source_uri"]
    )
    if not path.is_relative_to(root) or not path_exists:
        raise AnnotationConflictError("标注源文件不存在或越过资料根目录")
    return path, row["source_uri"]


def _resolve_local_path(root_value: str, source_uri: str) -> tuple[Path, Path, bool]:
    root = Path(root_value).expanduser().resolve()
    path = (root / source_uri).resolve()
    return root, path, path.is_file()


def render_pdf_page(path: Path, page_no: int, scale: float = 1.5) -> bytes:
    if path.suffix.lower() != ".pdf":
        raise AnnotationConflictError("只有 PDF 支持页面预览")
    document: Any = pymupdf.open(path)  # type: ignore[no-untyped-call]
    try:
        if not 1 <= page_no <= document.page_count:
            raise AnnotationConflictError("PDF 页码越界")
        matrix = pymupdf.Matrix(scale, scale)  # type: ignore[no-untyped-call]
        pixmap: Any = document[page_no - 1].get_pixmap(matrix=matrix, alpha=False)
        return bytes(pixmap.tobytes("png"))
    finally:
        document.close()
