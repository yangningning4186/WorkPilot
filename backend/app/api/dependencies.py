from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.db import get_db_session, session_factory
from app.core.queue import RunQueue, get_run_queue
from app.core.redis import redis_client
from app.core.run_bus import RedisRunBus, RunBus
from app.llm.audit import SqlLlmCallAudit
from app.llm.gateway import ModelGateway, build_model_gateway
from app.services.model_budget import build_cost_guard


def get_run_bus() -> RunBus:
    return RedisRunBus(redis_client)


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """SSE 流要在请求依赖结束后继续开会话, 不能复用请求作用域的 session。"""

    return session_factory


async def get_run_queue_dependency() -> RunQueue:
    return await get_run_queue()


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
