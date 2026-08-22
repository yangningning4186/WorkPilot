from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.core.config import Settings, get_settings
from app.core.db import DbSession as AsyncSession
from app.core.db import SessionFactory, get_db_session, session_factory
from app.core.queue import RunQueue, get_run_queue
from app.core.run_bus import RunBus, in_memory_run_bus
from app.llm_bootstrap import build_model_gateway
from app.platform.admin_sessions import AdminSessionStore
from app.rag.editor_permissions import EditorPermissionStore, InProcessEditorPermissionStore
from app.telemetry import default_telemetry_store
from app.telemetry.model_budget import build_cost_guard
from workpilot_ai.gateway import ModelGateway


def get_run_bus() -> RunBus:
    return in_memory_run_bus()


@lru_cache(maxsize=1)
def get_admin_session_store() -> AdminSessionStore:
    """进程内单例：会话表就在内存里，每次 new 一个等于每次都登出。"""
    return AdminSessionStore(ttl_s=get_settings().admin_session_ttl_s)


@lru_cache(maxsize=1)
def get_editor_permission_store() -> EditorPermissionStore:
    """进程内单例：授权表就在内存里，每次 new 一个等于每次都没授权。"""
    return InProcessEditorPermissionStore()


def get_session_factory() -> SessionFactory:
    """SSE 流要在请求依赖结束后继续开会话, 不能复用请求作用域的 session。"""

    return session_factory


async def get_run_queue_dependency() -> RunQueue:
    return await get_run_queue()


async def require_admin_session(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[AdminSessionStore, Depends(get_admin_session_store)],
) -> None:
    """`require_owner_identity` 的别名。

    demo 身份删掉之后，"登录了 admin" 和 "是 owner" 是同一件事，两个名字留着只是为了
    不去改几十个 `Depends(...)`。真正的判定在下面那一个函数里。
    """
    await require_owner_identity(request, settings, store)


async def require_owner_identity(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[AdminSessionStore, Depends(get_admin_session_store)],
) -> None:
    """本机 owner 之外没有第二种身份。

    demo 身份（匿名配额会话、`scope`/`demo_session_id` 那一整条参数链）已经删掉：
    这是单用户本机应用，CLAUDE.md 第一条就写了无多租户。放着一条走不通的分支，只会让
    每个仓储函数都多带两个永远等于 `("local_owner", None)` 的参数。

    两条入口：桌面壳的启动令牌（最外层 middleware 已恒时比较过），或浏览器里的
    admin Cookie。都没有就是 401。"""

    if getattr(request.state, "desktop_authenticated", False):
        return
    token = request.cookies.get(settings.admin_cookie_name)
    if token is None or not await store.validate(token):
        raise HTTPException(status_code=401, detail="需要先登录 owner")


async def require_editor_write_permission(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[EditorPermissionStore, Depends(get_editor_permission_store)],
) -> None:
    """写权限绑定 owner session，过期后必须由用户重新授权。"""

    token = request.cookies.get(settings.admin_cookie_name)
    if token is None:
        raise HTTPException(status_code=401, detail="需要先登录 owner")
    remaining = await store.ttl(token)
    if remaining <= 0:
        raise HTTPException(status_code=403, detail="尚未授予本地办公文档写权限或权限已过期")


async def get_model_gateway(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> AsyncIterator[ModelGateway]:
    settings = get_settings()
    _telemetry = default_telemetry_store()
    gateway = build_model_gateway(
        settings,
        audit_sink=_telemetry,
        # 费用闸门用独立 session, 不能随业务事务一起回滚。
        budget_guard=build_cost_guard(settings, _telemetry),
    )
    try:
        yield gateway
    finally:
        await gateway.aclose()
