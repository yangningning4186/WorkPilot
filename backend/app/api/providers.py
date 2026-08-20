"""本机模型 Provider 与密钥管理。"""

from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_owner_identity
from app.core.config import Settings, get_settings
from app.core.db import get_db_session
from app.cowork.provider_probe import probe_provider_profile
from app.cowork.provider_profiles import (
    create_provider_profile,
    delete_provider_profile,
    get_provider_profile,
    list_provider_profiles,
    provider_api_key,
    update_provider_profile,
)
from app.schemas.providers import (
    ProviderProbeResponse,
    ProviderProfileCreate,
    ProviderProfileListResponse,
    ProviderProfileResponse,
    ProviderProfileUpdate,
)
from app.security.secret_store import LocalSecretStore, SecretStoreError

router = APIRouter(
    prefix="/api/v1/providers",
    tags=["providers"],
    dependencies=[Depends(require_owner_identity)],
)
DbSession = Annotated[AsyncSession, Depends(get_db_session)]
RuntimeSettings = Annotated[Settings, Depends(get_settings)]


def _response(value: object) -> ProviderProfileResponse:
    return ProviderProfileResponse.model_validate(value, from_attributes=True)


def _store(settings: Settings) -> LocalSecretStore:
    try:
        return LocalSecretStore(settings.secret_store_key_path)
    except SecretStoreError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@router.get("", response_model=ProviderProfileListResponse)
async def get_providers(session: DbSession) -> ProviderProfileListResponse:
    items = await list_provider_profiles(session)
    return ProviderProfileListResponse(items=[_response(item.public()) for item in items])


@router.post("", response_model=ProviderProfileResponse, status_code=status.HTTP_201_CREATED)
async def post_provider(
    request: ProviderProfileCreate,
    session: DbSession,
    settings: RuntimeSettings,
) -> ProviderProfileResponse:
    try:
        created = await create_provider_profile(
            session,
            **request.model_dump(),
            secret_store=_store(settings),
        )
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Provider 名称已存在") from error
    return _response(created.public())


@router.patch("/{profile_id}", response_model=ProviderProfileResponse)
async def patch_provider(
    profile_id: UUID,
    request: ProviderProfileUpdate,
    session: DbSession,
    settings: RuntimeSettings,
) -> ProviderProfileResponse:
    try:
        updated = await update_provider_profile(
            session,
            profile_id=profile_id,
            changes=request.model_dump(exclude_unset=True),
            secret_store=_store(settings),
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Provider 不存在")
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Provider 名称已存在") from error
    return _response(updated.public())


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider_route(profile_id: UUID, session: DbSession) -> Response:
    if not await delete_provider_profile(session, profile_id):
        raise HTTPException(status_code=404, detail="Provider 不存在")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{profile_id}/probe", response_model=ProviderProbeResponse)
async def post_provider_probe(
    profile_id: UUID,
    session: DbSession,
    settings: RuntimeSettings,
) -> ProviderProbeResponse:
    profile = await get_provider_profile(session, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Provider 不存在")
    try:
        result = await probe_provider_profile(
            profile,
            api_key=provider_api_key(profile, _store(settings)),
            timeout_s=settings.model_timeout_s,
            trust_env=settings.model_trust_env,
        )
    except (httpx.HTTPError, ValueError, SecretStoreError) as error:
        raise HTTPException(
            status_code=502, detail="Provider 探测失败，请检查地址、密钥和网络"
        ) from error
    return ProviderProbeResponse(
        ok=True,
        provider=profile.provider,
        models=result.models,
        latency_ms=result.latency_ms,
        message=f"连接正常，发现 {len(result.models)} 个模型",
    )
