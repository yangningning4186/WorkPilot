from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_request_identity
from app.core.db import get_db_session
from app.schemas.library import LibraryResponse
from app.services.library import get_library_overview
from app.services.request_identity import RequestIdentity

# 只读一个聚合视图, 因此挂 demo session 而不是 admin: 资料库页是产品的一部分,
# 看得到"库里有什么、解析成不成功"是理解答案可信度的前提。
# 写侧(注册 source、触发同步、标注)仍然强制 admin, 在各自的路由上。
router = APIRouter(prefix="/api/v1/library", tags=["library"])


@router.get("", response_model=LibraryResponse)
async def get_library(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    _: Annotated[RequestIdentity, Depends(get_request_identity)],
    query: Annotated[str, Query(max_length=200)] = "",
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> LibraryResponse:
    return await get_library_overview(session, query=query, limit=limit)
