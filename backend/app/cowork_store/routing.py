"""Cowork 控制面存储入口。

**只有 SQLite 一种后端。** PostgreSQL 那条路是迁移期的双读通道，两边都跑通之后它
只是每个仓储函数里一段永远不执行的 `else`——而那段 `else` 会让人以为还有第二个
可选后端，实际上早就没有了。

`local_run_guard` 是原来 `SELECT ... FOR UPDATE` 行锁的替代：桌面版是单进程，
分片的 asyncio 锁就能串行化同一 run 的跨事务控制操作。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from app.cowork_store.base import CoworkStore


def cowork_store() -> CoworkStore:
    """按协议类型返回。

    具体适配器上的方法标注是 `Any`（SQLite 的行转记录没有静态类型），协议里才有精确
    的返回类型；调用方拿协议就不必在每个 return 上再写一次 cast。
    """

    from app.cowork_store.factory import local_cowork_stores

    return local_cowork_stores().state


@asynccontextmanager
async def local_run_guard(run_id: UUID) -> AsyncIterator[bool]:
    """串行化同一 run 的跨事务控制操作。"""

    from app.cowork_store.factory import local_cowork_stores

    # 分片锁是 SQLite 适配器的实现细节，不进通用协议。
    async with local_cowork_stores().state.run_lock(run_id):
        yield True
