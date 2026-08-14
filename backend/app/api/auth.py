from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from redis.exceptions import RedisError

from app.api.dependencies import get_admin_session_store, require_admin_session
from app.core.config import Settings, get_settings
from app.schemas.auth import AdminLoginRequest, AdminSessionResponse
from app.services.admin_sessions import AdminSessionStore, verify_admin_password

router = APIRouter(prefix="/api/v1/auth/admin", tags=["auth"])


def _cookie_secure(settings: Settings) -> bool:
    configured = settings.session_cookie_secure
    return settings.app_env == "production" if configured is None else configured


@router.post("/login", response_model=AdminSessionResponse)
async def login_admin(
    body: AdminLoginRequest,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[AdminSessionStore, Depends(get_admin_session_store)],
) -> AdminSessionResponse:
    if not settings.demo_admin_password_hash:
        raise HTTPException(status_code=503, detail="demo admin 尚未配置")
    if not verify_admin_password(body.password, settings.demo_admin_password_hash):
        raise HTTPException(status_code=401, detail="密码错误")
    try:
        token = await store.issue(ttl_s=settings.admin_session_ttl_s)
    except RedisError as error:
        raise HTTPException(status_code=503, detail="admin 会话服务不可用") from error
    response.set_cookie(
        key=settings.admin_cookie_name,
        value=token,
        max_age=settings.admin_session_ttl_s,
        httponly=True,
        secure=_cookie_secure(settings),
        samesite="lax",
        path="/",
    )
    return AdminSessionResponse(authenticated=True)


@router.get(
    "/session",
    response_model=AdminSessionResponse,
    dependencies=[Depends(require_admin_session)],
)
async def read_admin_session() -> AdminSessionResponse:
    return AdminSessionResponse(authenticated=True)


@router.post("/logout", status_code=204)
async def logout_admin(
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[AdminSessionStore, Depends(get_admin_session_store)],
) -> Response:
    token = request.cookies.get(settings.admin_cookie_name)
    if token is not None:
        try:
            await store.revoke(token)
        except RedisError as error:
            raise HTTPException(status_code=503, detail="admin 会话服务不可用") from error
    response.delete_cookie(
        settings.admin_cookie_name,
        httponly=True,
        secure=_cookie_secure(settings),
        samesite="lax",
        path="/",
    )
    response.status_code = 204
    return response
