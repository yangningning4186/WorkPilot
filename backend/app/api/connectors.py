"""连接器账户与 OAuth 生命周期 API。"""

from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.api.dependencies import require_owner_identity
from app.core.config import Settings, get_settings
from app.cowork.connectors import (
    ConnectorNameTakenError,
    create_connector_account,
    delete_connector_account,
    get_connector_account,
    list_connector_accounts,
    update_connector_account,
)
from app.cowork.oauth_connectors import begin_oauth, complete_oauth
from app.schemas.connectors import (
    ConnectorAccountCreate,
    ConnectorAccountListResponse,
    ConnectorAccountResponse,
    ConnectorAccountUpdate,
    OAuthStartRequest,
    OAuthStartResponse,
)
from app.security.secret_store import LocalSecretStore, SecretStoreError

router = APIRouter(
    prefix="/api/v1/connectors",
    tags=["connectors"],
    dependencies=[Depends(require_owner_identity)],
)
callback_router = APIRouter(prefix="/api/v1/connectors", tags=["connectors"])
RuntimeSettings = Annotated[Settings, Depends(get_settings)]


def _response(value: object) -> ConnectorAccountResponse:
    return ConnectorAccountResponse.model_validate(value, from_attributes=True)


def _store(settings: Settings) -> LocalSecretStore:
    try:
        return LocalSecretStore(settings.secret_store_key_path)
    except SecretStoreError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@router.get("", response_model=ConnectorAccountListResponse)
def get_connectors(settings: RuntimeSettings) -> ConnectorAccountListResponse:
    items = list_connector_accounts(settings)
    return ConnectorAccountListResponse(items=[_response(item.public()) for item in items])


@router.post("", response_model=ConnectorAccountResponse, status_code=status.HTTP_201_CREATED)
def post_connector(
    request: ConnectorAccountCreate,
    settings: RuntimeSettings,
) -> ConnectorAccountResponse:
    try:
        created = create_connector_account(
            settings,
            **request.model_dump(),
            secret_store=_store(settings),
        )
    except ConnectorNameTakenError as error:
        raise HTTPException(status_code=409, detail="同类连接器名称已存在") from error
    return _response(created.public())


@router.patch("/{account_id}", response_model=ConnectorAccountResponse)
def patch_connector(
    account_id: UUID,
    request: ConnectorAccountUpdate,
    settings: RuntimeSettings,
) -> ConnectorAccountResponse:
    try:
        updated = update_connector_account(
            settings,
            account_id=account_id,
            changes=request.model_dump(exclude_unset=True),
            secret_store=_store(settings),
        )
    except ConnectorNameTakenError as error:
        raise HTTPException(status_code=409, detail="同类连接器名称已存在") from error
    if updated is None:
        raise HTTPException(status_code=404, detail="连接器不存在")
    return _response(updated.public())


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connector_route(account_id: UUID, settings: RuntimeSettings) -> Response:
    if not delete_connector_account(settings, account_id):
        raise HTTPException(status_code=404, detail="连接器不存在")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{account_id}/oauth/start", response_model=OAuthStartResponse)
async def post_oauth_start(
    account_id: UUID,
    request: OAuthStartRequest,
    settings: RuntimeSettings,
) -> OAuthStartResponse:
    account = get_connector_account(settings, account_id)
    if account is None or not account.enabled:
        raise HTTPException(status_code=404, detail="连接器不存在或已停用")
    try:
        result = await begin_oauth(
            settings=settings, account=account, redirect_uri=request.redirect_uri
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return OAuthStartResponse.model_validate(result, from_attributes=True)


@callback_router.get("/oauth/callback", response_model=ConnectorAccountResponse)
async def get_oauth_callback(
    settings: RuntimeSettings,
    state_value: Annotated[str, Query(alias="state", min_length=32, max_length=256)],
    code: Annotated[str | None, Query(max_length=4096)] = None,
    error: Annotated[str | None, Query(max_length=512)] = None,
) -> ConnectorAccountResponse:
    if error is not None:
        raise HTTPException(status_code=400, detail=f"OAuth 授权被拒绝：{error}")
    if not code:
        raise HTTPException(status_code=422, detail="OAuth 回调缺少 code")
    try:
        account = await complete_oauth(
            settings=settings,
            state=state_value,
            code=code,
            secret_store=_store(settings),
            timeout_s=settings.cowork_web_timeout_s,
            trust_env=False,
        )
    except ValueError as exchange_error:
        raise HTTPException(status_code=422, detail=str(exchange_error)) from exchange_error
    except httpx.HTTPError as exchange_error:
        raise HTTPException(
            status_code=502, detail="OAuth 服务请求失败，请检查网络后重试"
        ) from exchange_error
    return _response(account.public())
