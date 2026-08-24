"""LLM 调用与费用预留的 SQLite 适配器。

契约、schema 与成本口径在 `packages/workpilot-telemetry/`（一个依赖表为空的包）；
这里只有"写进哪个库、哪张表"。存储从 PostgreSQL 换成 SQLite 之后，这条分界线一行
没变——那正是当初把契约拆成独立包要买的东西。
"""

from app.telemetry.sqlite import SqliteTelemetryStore

_store: SqliteTelemetryStore | None = None


def default_telemetry_store() -> "SqliteTelemetryStore":
    """进程内单例。

    审计与费用闸门要在同一个库文件上，而且这个 store 自带写锁——每次现造一个，
    那把锁就形同虚设，并发预留会同时开写事务互相撞 SQLITE_BUSY。
    """
    global _store
    if _store is None:
        from app.core.config import get_settings

        settings = get_settings()
        _store = SqliteTelemetryStore(settings.cowork_data_path.expanduser() / "telemetry.db")
    return _store


async def initialize_telemetry_store() -> "SqliteTelemetryStore":
    store = default_telemetry_store()
    await store.initialize()
    return store
