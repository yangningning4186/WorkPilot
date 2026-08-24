"""轻量 Persona 目录；选择结果由 conversation runtime API 持久化。"""

from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import require_owner_identity
from app.core.config import Settings, get_settings
from app.core.db import DbSession as AsyncSession
from app.core.db import get_db_session
from app.cowork.permissions import list_session_roots
from app.cowork.personas import PROJECT_PERSONAS_RELATIVE, load_persona_catalog
from app.schemas.personas import PersonaListResponse, PersonaResponse

router = APIRouter(
    prefix="/api/v1/personas",
    tags=["personas"],
    dependencies=[Depends(require_owner_identity)],
)


@router.get("", response_model=PersonaListResponse)
async def get_personas(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    conversation_id: Annotated[UUID | None, Query()] = None,
) -> PersonaListResponse:
    roots = (
        []
        if conversation_id is None
        else await list_session_roots(session, conversation_id=conversation_id)
    )
    project_roots = tuple(Path(item.canonical_path) for item in roots)
    catalog = load_persona_catalog(settings, project_roots=project_roots)
    return PersonaListResponse(
        items=[PersonaResponse.model_validate(item.public()) for item in catalog.personas],
        errors=list(catalog.errors),
        project_paths=[str(root / PROJECT_PERSONAS_RELATIVE) for root in project_roots],
    )
