"""事务边界的残余形态。

**PostgreSQL 已经退役。** `app/` 里没有任何一条 SQL 了：Cowork 控制面在
`~/.workpilot/cowork.db`，消息在 JSONL，审计与费用在 `telemetry.db`，Provider 与
连接器在 0600 的 JSON 文件里，Skill 候选在目录里。每一处都用自己的短事务管边界。

但 `session` 这个形参还穿在上百个函数签名上。一次性摘掉它是一场纯机械的大改，
风险不在设计而在手滑，所以分两步：**先让它无法再连数据库**，签名清理留作后续。

`DbSession` 就是那个惰性对象——`commit()` / `rollback()` 什么也不做，`execute()`
直接抛错。于是"谁又偷偷写了一条 SQL"会在第一次运行时当场炸出来，而不是安静地
把 PostgreSQL 依赖带回来。
"""

from collections.abc import AsyncIterator, Callable
from typing import Any, Never


class DbSession:
    """不连接任何数据库的事务壳。"""

    async def execute(self, *args: Any, **kwargs: Any) -> Never:
        raise RuntimeError(
            "PostgreSQL 已退役，不能再执行 SQL。"
            "Cowork 状态走 app.cowork_store.routing.cowork_store()，"
            "审计与费用走 app.telemetry，配置与凭据走 app.core.private_json。"
        )

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def __aenter__(self) -> "DbSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


# 调用点仍然写 `async with session_factory() as session:`，所以它得可调用且
# 返回一个异步上下文管理器——DbSession 自己就是。
SessionFactory = Callable[[], DbSession]


def session_factory() -> DbSession:
    return DbSession()


async def get_db_session() -> AsyncIterator[DbSession]:
    yield DbSession()


async def close_database() -> None:
    return None
