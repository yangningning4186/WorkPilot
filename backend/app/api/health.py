from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.cowork_store.routing import cowork_store

router = APIRouter(prefix="/health", tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


@router.get("/live", response_model=HealthResponse)
async def live() -> HealthResponse:
    return HealthResponse()


@router.get("/ready", response_model=HealthResponse)
async def ready() -> HealthResponse:
    """就绪 = 本机存储能开。

    PostgreSQL 和 Redis 都退役了，剩下的唯一外部依赖就是磁盘上那个 SQLite 文件——
    权限不对或目录不存在时它打不开，那才是"没就绪"。
    """

    try:
        await cowork_store().list_queued_runs(limit=1)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="本机存储尚未就绪") from exc
    return HealthResponse()
