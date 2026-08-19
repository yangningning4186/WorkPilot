import html
from pathlib import Path
from typing import Annotated
from uuid import UUID

from docx import Document
from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import FileResponse, HTMLResponse
from openpyxl import load_workbook  # type: ignore[import-untyped]
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_owner_identity
from app.core.config import Settings, get_settings
from app.core.db import get_db_session
from app.schemas.cowork import (
    ArtifactListResponse,
    ArtifactResponse,
    CapabilityGrantCreate,
    CapabilityGrantListResponse,
    CapabilityGrantResponse,
    SessionRootCreate,
    SessionRootListResponse,
    SessionRootResponse,
)
from app.services.artifact_formats import TEXT_ARTIFACT_SUFFIXES
from app.services.artifacts import ArtifactRegistrationError, list_artifacts, resolve_artifact_file
from app.services.cowork_permissions import (
    CapabilityDeniedError,
    ConversationNotFoundError,
    CoworkPermissionError,
    SessionRootNotFoundError,
    create_session_root,
    grant_capability,
    list_capability_grants,
    list_session_roots,
    revoke_capability_grant,
    revoke_session_root,
)


async def require_cowork_enabled(
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    if not settings.cowork_enabled:
        raise HTTPException(status_code=404, detail="Cowork 功能尚未启用")


router = APIRouter(
    prefix="/api/v1/cowork",
    tags=["cowork"],
    dependencies=[Depends(require_owner_identity), Depends(require_cowork_enabled)],
)
DbSession = Annotated[AsyncSession, Depends(get_db_session)]
_PREVIEW_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def _root_response(value: object) -> SessionRootResponse:
    return SessionRootResponse.model_validate(value, from_attributes=True)


def _grant_response(value: object) -> CapabilityGrantResponse:
    return CapabilityGrantResponse.model_validate(value, from_attributes=True)


def _not_found(error: ConversationNotFoundError) -> HTTPException:
    return HTTPException(status_code=404, detail="Cowork 会话不存在")


@router.get(
    "/sessions/{conversation_id}/roots", response_model=SessionRootListResponse
)
async def get_session_roots(
    conversation_id: UUID, session: DbSession
) -> SessionRootListResponse:
    try:
        items = await list_session_roots(session, conversation_id=conversation_id)
    except ConversationNotFoundError as error:
        raise _not_found(error) from error
    return SessionRootListResponse(items=[_root_response(item) for item in items])


@router.post(
    "/sessions/{conversation_id}/roots",
    response_model=SessionRootResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_session_root(
    conversation_id: UUID, request: SessionRootCreate, session: DbSession
) -> SessionRootResponse:
    try:
        root = await create_session_root(
            session,
            conversation_id=conversation_id,
            requested_path=request.path,
            access_mode=request.access_mode,
            label=request.label,
        )
    except ConversationNotFoundError as error:
        raise _not_found(error) from error
    except (CoworkPermissionError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    await session.commit()
    return _root_response(root)


@router.delete(
    "/sessions/{conversation_id}/roots/{root_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_session_root(
    conversation_id: UUID, root_id: UUID, session: DbSession
) -> Response:
    try:
        deleted = await revoke_session_root(
            session, conversation_id=conversation_id, root_id=root_id
        )
    except ConversationNotFoundError as error:
        raise _not_found(error) from error
    if not deleted:
        raise HTTPException(status_code=404, detail="会话目录不存在")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/sessions/{conversation_id}/grants", response_model=CapabilityGrantListResponse
)
async def get_capability_grants(
    conversation_id: UUID, session: DbSession
) -> CapabilityGrantListResponse:
    try:
        items = await list_capability_grants(session, conversation_id=conversation_id)
    except ConversationNotFoundError as error:
        raise _not_found(error) from error
    return CapabilityGrantListResponse(items=[_grant_response(item) for item in items])


@router.post(
    "/sessions/{conversation_id}/grants",
    response_model=CapabilityGrantResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_capability_grant(
    conversation_id: UUID, request: CapabilityGrantCreate, session: DbSession
) -> CapabilityGrantResponse:
    try:
        grant = await grant_capability(
            session,
            conversation_id=conversation_id,
            capability=request.capability,
            session_root_id=request.session_root_id,
            expires_in_s=request.expires_in_s,
        )
    except ConversationNotFoundError as error:
        raise _not_found(error) from error
    except SessionRootNotFoundError as error:
        raise HTTPException(status_code=404, detail="会话目录不存在") from error
    except (CapabilityDeniedError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    await session.commit()
    return _grant_response(grant)


@router.delete(
    "/sessions/{conversation_id}/grants/{grant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_capability_grant(
    conversation_id: UUID, grant_id: UUID, session: DbSession
) -> Response:
    try:
        deleted = await revoke_capability_grant(
            session, conversation_id=conversation_id, grant_id=grant_id
        )
    except ConversationNotFoundError as error:
        raise _not_found(error) from error
    if not deleted:
        raise HTTPException(status_code=404, detail="能力授权不存在")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/sessions/{conversation_id}/artifacts", response_model=ArtifactListResponse
)
async def get_artifacts(
    conversation_id: UUID, session: DbSession
) -> ArtifactListResponse:
    try:
        items = await list_artifacts(session, conversation_id=conversation_id)
    except ConversationNotFoundError as error:
        raise _not_found(error) from error
    return ArtifactListResponse(
        items=[ArtifactResponse.model_validate(item, from_attributes=True) for item in items]
    )


@router.get("/artifacts/{artifact_id}/preview")
async def get_artifact_preview(
    artifact_id: UUID,
    session: DbSession,
) -> Response:
    try:
        resolved = await resolve_artifact_file(session, artifact_id=artifact_id)
    except ArtifactRegistrationError as error:
        raise HTTPException(status_code=410, detail=str(error)) from error
    if resolved is None:
        raise HTTPException(status_code=404, detail="交付物不存在")
    artifact, path = resolved
    suffix = path.suffix.casefold()
    # 预览类型只能由经过白名单校验的扩展名决定，不能信任模型登记的 mime_type。
    if suffix == ".pdf":
        return FileResponse(
            path,
            media_type="application/pdf",
            headers={
                **_PREVIEW_SECURITY_HEADERS,
                "Content-Disposition": f'inline; filename="{_safe_preview_name(path)}"',
            },
        )
    if suffix == ".docx":
        body = _docx_preview(path)
    elif suffix == ".xlsx":
        body = _xlsx_preview(path)
    elif suffix in TEXT_ARTIFACT_SUFFIXES:
        if path.stat().st_size > 5 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="交付物过大，无法在线预览")
        body = f"<pre>{html.escape(path.read_text(encoding='utf-8', errors='replace'))}</pre>"
    else:
        raise HTTPException(status_code=415, detail="该交付物格式不支持在线预览")
    document = (
        "<!doctype html><meta charset='utf-8'><style>"
        "body{font:15px/1.65 system-ui;color:#26332f;padding:32px;max-width:960px;margin:auto}"
        "table{border-collapse:collapse;width:100%}td,th{border:1px solid #d9dedb;padding:6px 8px}"
        "pre{white-space:pre-wrap;word-break:break-word}</style>"
        f"<title>{html.escape(artifact.title)}</title><h1>{html.escape(artifact.title)}</h1>{body}"
    )
    return HTMLResponse(
        document,
        headers=_PREVIEW_SECURITY_HEADERS,
    )


def _docx_preview(path: Path) -> str:
    document = Document(str(path))
    blocks: list[str] = []
    for paragraph in document.paragraphs[:1000]:
        text = html.escape(paragraph.text)
        if not text:
            continue
        style = paragraph.style.name.casefold() if paragraph.style is not None else ""
        tag = "h2" if "heading" in style else "p"
        blocks.append(f"<{tag}>{text}</{tag}>")
    return "".join(blocks)


def _xlsx_preview(path: Path) -> str:
    workbook = load_workbook(path, read_only=True, data_only=False)
    try:
        blocks: list[str] = []
        for sheet in workbook.worksheets[:5]:
            blocks.append(f"<h2>{html.escape(sheet.title)}</h2><table>")
            for row in sheet.iter_rows(min_row=1, max_row=100, max_col=20, values_only=True):
                blocks.append("<tr>" + "".join(f"<td>{html.escape(str(value if value is not None else ''))}</td>" for value in row) + "</tr>")
            blocks.append("</table>")
        return "".join(blocks)
    finally:
        workbook.close()


def _safe_preview_name(path: Path) -> str:
    return "".join(character for character in path.name if character.isalnum() or character in "._-") or "artifact"
