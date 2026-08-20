from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.db import get_db_session, session_factory
from app.core.queue import RunQueue, get_run_queue
from app.core.redis import redis_client
from app.core.run_bus import RedisRunBus, RunBus, in_memory_run_bus
from app.llm.audit import SqlLlmCallAudit
from app.llm.gateway import ModelGateway, build_model_gateway
from app.services.admin_sessions import AdminSessionStore, RedisAdminSessionStore
from app.services.demo_sessions import DemoSession, resolve_demo_session
from app.services.editor_permissions import EditorPermissionStore, RedisEditorPermissionStore
from app.services.model_budget import build_cost_guard
from app.services.rate_limit import IpRateLimiter, RedisIpRateLimiter
from app.services.request_identity import RequestIdentity


def get_run_bus() -> RunBus:
    if get_settings().run_bus_backend == "in_process":
        return in_memory_run_bus()
    return RedisRunBus(redis_client)


def get_admin_session_store() -> AdminSessionStore:
    return RedisAdminSessionStore(redis_client)


def get_editor_permission_store() -> EditorPermissionStore:
    return RedisEditorPermissionStore(redis_client)


def get_ip_rate_limiter() -> IpRateLimiter:
    return RedisIpRateLimiter(redis_client)


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """SSE 流要在请求依赖结束后继续开会话, 不能复用请求作用域的 session。"""

    return session_factory


async def get_run_queue_dependency() -> RunQueue:
    return await get_run_queue()


async def get_demo_session(
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DemoSession:
    """解析或签发匿名 session；原始 token 只进入 HttpOnly cookie。"""

    cookie_name = settings.session_cookie_name
    resolved = await resolve_demo_session(
        session,
        cookie_token=request.cookies.get(cookie_name),
        ttl_s=settings.session_ttl_s,
    )
    if resolved.cookie_token is not None:
        secure = settings.session_cookie_secure
        response.set_cookie(
            key=cookie_name,
            value=resolved.cookie_token,
            max_age=settings.session_ttl_s,
            httponly=True,
            secure=settings.app_env == "production" if secure is None else secure,
            samesite="lax",
            path="/",
        )
    return resolved.session


async def require_admin_session(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[AdminSessionStore, Depends(get_admin_session_store)],
) -> None:
    """要求有效 admin Cookie；未配置密码也绝不隐式放行。"""

    if getattr(request.state, "desktop_authenticated", False):
        return

    token = request.cookies.get(settings.admin_cookie_name)
    if token is None:
        raise HTTPException(status_code=401, detail="需要 demo admin 登录")
    try:
        valid = await store.validate(token)
    except RedisError as error:
        raise HTTPException(status_code=503, detail="admin 会话服务不可用") from error
    if not valid:
        raise HTTPException(status_code=401, detail="admin 会话无效或已过期")


async def get_request_identity(
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[AdminSessionStore, Depends(get_admin_session_store)],
) -> RequestIdentity:
    """登录 admin 映射为 owner；没有 admin Cookie 才签发匿名 demo 身份。"""

    # 桌面启动令牌已由最外层 middleware 恒时比较过；它就是单用户
    # 本机应用的 owner session。这样 Tauri 不需要再保存一份管理员口令。
    if getattr(request.state, "desktop_authenticated", False):
        return RequestIdentity(scope="local_owner")

    admin_token = request.cookies.get(settings.admin_cookie_name)
    if admin_token is not None:
        try:
            valid = await store.validate(admin_token)
        except RedisError as error:
            raise HTTPException(status_code=503, detail="admin 会话服务不可用") from error
        if not valid:
            # 不能静默降级成 demo，否则用户以为这轮会被记住，实际却被匿名处理。
            raise HTTPException(status_code=401, detail="admin 会话无效或已过期")
        return RequestIdentity(scope="local_owner")

    resolved = await resolve_demo_session(
        session,
        cookie_token=request.cookies.get(settings.session_cookie_name),
        ttl_s=settings.session_ttl_s,
    )
    if resolved.cookie_token is not None:
        secure = settings.session_cookie_secure
        response.set_cookie(
            key=settings.session_cookie_name,
            value=resolved.cookie_token,
            max_age=settings.session_ttl_s,
            httponly=True,
            secure=settings.app_env == "production" if secure is None else secure,
            samesite="lax",
            path="/",
        )
    return RequestIdentity(scope="demo", demo_session_id=resolved.session.id)


async def require_owner_identity(
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
) -> RequestIdentity:
    if not identity.is_owner:
        raise HTTPException(status_code=401, detail="需要先登录 owner")
    return identity


async def require_editor_write_permission(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[EditorPermissionStore, Depends(get_editor_permission_store)],
) -> None:
    """写权限绑定 owner session，过期后必须由用户重新授权。"""

    token = request.cookies.get(settings.admin_cookie_name)
    if token is None:
        raise HTTPException(status_code=401, detail="需要先登录 owner")
    try:
        remaining = await store.ttl(token)
    except RedisError as error:
        raise HTTPException(status_code=503, detail="文档权限服务不可用") from error
    if remaining <= 0:
        raise HTTPException(status_code=403, detail="尚未授予本地办公文档写权限或权限已过期")


async def enforce_ip_rate_limit(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    limiter: Annotated[IpRateLimiter, Depends(get_ip_rate_limiter)],
) -> None:
    """生产默认开启；开发/测试可显式开启，避免本地测试互相污染桶状态。"""

    configured = settings.ip_rate_limit_enabled
    enabled = settings.app_env == "production" if configured is None else configured
    if not enabled:
        return
    ip = request.client.host if request.client is not None else "unknown"
    try:
        decision = await limiter.consume(
            ip,
            rate_per_minute=settings.ip_rate_limit_per_minute,
            burst=settings.ip_rate_limit_burst,
        )
    except RedisError as error:
        raise HTTPException(status_code=503, detail="请求限流服务不可用") from error
    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail="请求过于频繁, 请稍后重试",
            headers={"Retry-After": str(decision.retry_after_s)},
        )


async def get_model_gateway(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> AsyncIterator[ModelGateway]:
    settings = get_settings()
    gateway = build_model_gateway(
        settings,
        audit_sink=SqlLlmCallAudit(session),
        # 费用闸门用独立 session, 不能随业务事务一起回滚。
        budget_guard=build_cost_guard(settings, session_factory),
    )
    try:
        yield gateway
    finally:
        await gateway.aclose()
