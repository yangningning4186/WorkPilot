from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db_session
from app.llm.audit import SqlLlmCallAudit
from app.llm.gateway import ModelGateway, build_model_gateway


async def get_model_gateway(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> AsyncIterator[ModelGateway]:
    gateway = build_model_gateway(get_settings(), audit_sink=SqlLlmCallAudit(session))
    try:
        yield gateway
    finally:
        await gateway.aclose()
