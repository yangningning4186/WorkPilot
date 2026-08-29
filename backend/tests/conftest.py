"""测试装置。

PostgreSQL 退役之后这里没有数据库夹具了：每个用例自带一个空的本机 SQLite 库，
`db_session` 只是一个不连接任何东西的事务壳（见 `app/core/db.py`）。
"""

import os
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from uuid import UUID

import pytest
import pytest_asyncio

# 只剩 SQLite 一种后端。
os.environ.setdefault("COWORK_STORE_BACKEND", "sqlite")

from app.core.config import get_settings
from app.core.db import DbSession


@pytest.fixture
def db_session() -> DbSession:
    """惰性事务壳。

    上百个仓储函数还带着 `session` 形参（摘掉它是一场纯机械的大改，单独做）。
    这个对象不连接任何数据库；谁真去 execute 一条 SQL，会当场抛错。
    """

    return DbSession()


@pytest.fixture
def db_engine() -> None:
    """还有用例在签名里要它；PostgreSQL 没了之后它不再指向任何东西。"""

    return None


@pytest_asyncio.fixture(autouse=True)
async def local_cowork_store(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """每个用例一个干净的本机 SQLite 库。

    Cowork 控制面只剩这一种后端，所以这是全局 autouse 的：没有它，任何碰到 run、
    会话、权限或记忆的用例都会撞上"本地 store 尚未初始化"。数据落在用例自己的
    tmp_path，用例之间互不可见——比共享一个库再逐表 TRUNCATE 干净。
    """

    from app.cowork_store.factory import close_local_cowork_stores, initialize_local_cowork_stores

    # 刻意不放在用例的 tmp_path 里：扫目录的用例会把这个库当成待扫描的文件。
    state_path = tmp_path_factory.mktemp("cowork-state")
    monkeypatch.setenv("COWORK_DATA_PATH", str(state_path))
    # Approval evidence keys are derived from SecretStore and must be isolated just like the
    # Cowork DB.  Never let a test create or reuse the desktop user's real master key.
    monkeypatch.setenv("SECRET_STORE_KEY_PATH", str(state_path / "secrets" / "master.key"))
    # 测试 Provider 的默认上下文只有 32K；生产 registry 改为全量 schema 后，保留 8K
    # 输出会让固定前缀刚好越界。测试只验证运行时协议，2K 输出足够且不裁剪 schema。
    monkeypatch.setenv("COWORK_DECISION_MAX_TOKENS", "2048")
    # Skill 蒸馏队列也是持久状态。若只隔离 cowork.db，跑测试会把数百条合成 run
    # 写进桌面端的真实后台队列，随后占满 worker、饿死用户任务。
    monkeypatch.setenv("COWORK_SKILL_CANDIDATES_PATH", str(state_path / "skills-candidates"))
    get_settings.cache_clear()
    await close_local_cowork_stores()
    await initialize_local_cowork_stores(get_settings())
    yield
    await close_local_cowork_stores()
    get_settings.cache_clear()


def iso_ago(seconds: int) -> str:
    """和 SQLite store 完全一致的时间戳格式，用于把租约手工推到过去。"""

    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat(timespec="microseconds")


@pytest.fixture
def store_sql() -> "Callable[..., list[dict[str, object]]]":
    """直接查本机 cowork.db。

    用例里那些"落库了没有"的断言原来打在 PostgreSQL 上；表搬到 SQLite 之后语义一样，
    只是换了个库。保留直查而不是改成走仓储函数——断言存储状态就该绕开被测的那层。
    """

    def run(sql: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
        # store 写的是 `_iso()`：带微秒 + `+00:00`。SQLite 的时间比较是字典序，
        # 用 `datetime('now')` 生成的 `2026-08-22 12:00:00` 排在它前面，看起来
        # 永远没过期。所以这里统一用同一个格式化函数。
        from app.cowork_store.factory import local_cowork_stores

        connection = sqlite3.connect(local_cowork_stores().state.path)
        connection.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]
        finally:
            connection.commit()
            connection.close()

    return run


@pytest_asyncio.fixture
async def message_status() -> "Callable[..., Awaitable[list[str]]]":
    """会话消息在 JSONL 里，不在 SQLite——按 run 取它们的状态。"""

    async def run(conversation_id: UUID, run_id: UUID, role: str) -> list[str]:
        from app.cowork_store.factory import local_cowork_stores

        records = await local_cowork_stores().conversations.read(conversation_id)
        return [
            item.status for item in records if item.role == role and str(item.run_id) == str(run_id)
        ]

    return run
