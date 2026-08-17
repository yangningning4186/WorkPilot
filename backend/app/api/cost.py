from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_admin_session
from app.core.config import Settings, get_settings
from app.core.db import get_db_session
from app.schemas.cost import CostOverviewResponse
from app.services.cost_overview import get_cost_overview

# 强制 admin：这个页面暴露的是运营信息——档位分布、GPU 单价与来源、跑批标签。
# 资料库页挂 demo session 是因为它是产品的一部分；成本不是, 演示时也不该被看到。
router = APIRouter(
    prefix="/api/v1/cost",
    tags=["cost"],
    dependencies=[Depends(require_admin_session)],
)


@router.get("/overview", response_model=CostOverviewResponse)
async def get_overview(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    label: Annotated[str | None, Query(max_length=200)] = None,
) -> CostOverviewResponse:
    return await get_cost_overview(session, settings=settings, days=days, label=label)
