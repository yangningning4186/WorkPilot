"""Cowork 控制面后端路由。

PostgreSQL 兼容路径保留在现有 repository 中，SQLite 模式则在进入任何 SQL 前
切到本地 Store。这样迁移期间可以逐模块双读，且 RAG 的数据库 session 不会被冒充
成 Cowork store。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from uuid import UUID

from app.core.config import get_settings

if TYPE_CHECKING:
    from app.cowork_store.base import CoworkStore


def configured_cowork_store() -> CoworkStore | None:
    if get_settings().cowork_store_backend != "sqlite":
        return None
    from app.cowork_store.factory import local_cowork_stores

    return local_cowork_stores().state


@asynccontextmanager
async def local_run_guard(run_id: UUID) -> AsyncIterator[bool]:
    """SQLite 单进程模式下串行化跨事务的同-run 控制操作。"""

    store = configured_cowork_store()
    if store is None:
        yield False
        return
    # configured SQLite adapter 的具体类型提供固定数量的分片锁；该细节不进入
    # 通用 CoworkStore 协议，PostgreSQL 仍使用行锁。
    lock = store.run_lock(run_id)  # type: ignore[attr-defined]
    async with lock:
        yield True
