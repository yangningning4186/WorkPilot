"""连接器账户与 OAuth 生命周期 API。"""

from html import escape
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import HTMLResponse

from app.api.dependencies import require_owner_identity
from app.core.config import Settings, get_settings
from app.cowork.connector_descriptors import get_connector_descriptor, list_connector_descriptors
from app.cowork.connectors import (
    ConnectorNameTakenError,
    create_connector_account,
    delete_connector_account,
    get_connector_account,
    list_connector_accounts,
    update_connector_account,
)
from app.cowork.oauth_connectors import begin_oauth, complete_oauth, reject_oauth
from app.schemas.connectors import (
    ConnectorAccountCreate,
    ConnectorAccountListResponse,
    ConnectorAccountResponse,
    ConnectorAccountUpdate,
    ConnectorDescriptorListResponse,
    ConnectorDescriptorResponse,
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
    response = ConnectorAccountResponse.model_validate(value, from_attributes=True)
    descriptor = get_connector_descriptor(response.kind)
    return response.model_copy(update={"capabilities": list(descriptor.capabilities)})


def _store(settings: Settings) -> LocalSecretStore:
    try:
        return LocalSecretStore(settings.secret_store_key_path)
    except SecretStoreError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


def _oauth_browser_page(
    *, title: str, detail: str, ok: bool, status_code: int = 200
) -> HTMLResponse:
    """系统浏览器里的 OAuth 终点；账户状态由客户端轮询，不把账户 JSON 留给用户。"""

    tone = "#18745a" if ok else "#a14f45"
    mark = "✓" if ok else "!"
    safe_title = escape(title)
    safe_detail = escape(detail)
    return HTMLResponse(
        status_code=status_code,
        content=f"""<!doctype html>
<html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>{safe_title} · WorkPilot</title><style>
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#f4f6f5;color:#25312c;font-family:-apple-system,BlinkMacSystemFont,\"Segoe UI\",sans-serif}}
main{{width:min(430px,calc(100% - 32px));padding:38px;border:1px solid #dce3df;border-radius:22px;background:#fff;box-shadow:0 24px 70px rgba(31,49,41,.12);text-align:center}}
i{{display:grid;width:52px;height:52px;margin:0 auto 20px;place-items:center;border-radius:16px;background:color-mix(in srgb,{tone} 11%,white);color:{tone};font-size:25px;font-style:normal;font-weight:700}}
h1{{margin:0 0 9px;font-size:24px;letter-spacing:-.035em}}p{{margin:0;color:#718078;font-size:14px;line-height:1.7}}small{{display:block;margin-top:24px;color:#9aa39f;font-size:12px}}
</style></head><body><main><i>{mark}</i><h1>{safe_title}</h1><p>{safe_detail}</p><small>现在可以关闭此页面，返回 WorkPilot</small></main></body></html>""",
    )


@router.get("", response_model=ConnectorAccountListResponse)
def get_connectors(settings: RuntimeSettings) -> ConnectorAccountListResponse:
    items = list_connector_accounts(settings)
    return ConnectorAccountListResponse(items=[_response(item.public()) for item in items])


@router.get("/catalog", response_model=ConnectorDescriptorListResponse)
def get_connector_catalog() -> ConnectorDescriptorListResponse:
    """前端和运行时共用的连接器目录；这里不再维护第二份平台常量。"""

    return ConnectorDescriptorListResponse(
        items=[
            ConnectorDescriptorResponse.model_validate(item.public())
            for item in list_connector_descriptors()
        ]
    )


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


@callback_router.get("/oauth/callback", response_class=HTMLResponse)
async def get_oauth_callback(
    settings: RuntimeSettings,
    state_value: Annotated[str, Query(alias="state", min_length=32, max_length=256)],
    code: Annotated[str | None, Query(max_length=4096)] = None,
    error: Annotated[str | None, Query(max_length=512)] = None,
) -> HTMLResponse:
    if error is not None:
        reject_oauth(settings=settings, state=state_value, reason=error)
        return _oauth_browser_page(
            title="授权未完成",
            detail=f"服务方拒绝了这次授权：{error}",
            ok=False,
            status_code=400,
        )
    if not code:
        reject_oauth(settings=settings, state=state_value, reason="回调缺少 code")
        return _oauth_browser_page(
            title="授权信息不完整",
            detail="回调中没有授权码，请返回 WorkPilot 后重新发起。",
            ok=False,
            status_code=422,
        )
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
        return _oauth_browser_page(
            title="授权未完成",
            detail=str(exchange_error),
            ok=False,
            status_code=422,
        )
    except httpx.HTTPError:
        return _oauth_browser_page(
            title="连接服务失败",
            detail="OAuth 服务请求失败，请检查网络后重试。",
            ok=False,
            status_code=502,
        )
    return _oauth_browser_page(
        title="授权完成",
        detail=f"{account.external_account_name or account.name} 已安全连接。",
        ok=True,
    )
