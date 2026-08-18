"""Arq worker 入口: uv run arq app.worker.main.WorkerSettings"""

from typing import Any, ClassVar

from arq import cron

from app.core.config import get_settings
from app.core.db import close_database, session_factory
from app.core.logging import configure_logging
from app.core.queue import redis_settings
from app.core.redis import close_redis, redis_client
from app.core.run_bus import RedisRunBus
from app.mcp.client import McpClientManager
from app.mcp.config import load_mcp_configuration
from app.worker.answer_run import answer_run
from app.worker.cowork_run import cowork_run
from app.worker.maintenance import cost_sweeper_tick, memory_dispatch_tick, watchdog_tick
from app.worker.memory_run import memory_extraction_job
from app.worker.review_run import review_run


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    ctx["settings"] = settings
    ctx["session_factory"] = session_factory
    ctx["bus"] = RedisRunBus(redis_client)
    ctx["mcp_manager"] = McpClientManager(
        load_mcp_configuration(settings.cowork_mcp_config_path),
        connect_timeout_s=settings.cowork_mcp_connect_timeout_s,
        call_timeout_s=settings.cowork_mcp_call_timeout_s,
        result_max_chars=settings.cowork_mcp_result_max_chars,
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    manager = ctx.get("mcp_manager")
    if isinstance(manager, McpClientManager):
        await manager.aclose()
    await close_redis()
    await close_database()


class WorkerSettings:
    functions: ClassVar = [answer_run, review_run, cowork_run, memory_extraction_job]
    cron_jobs: ClassVar = [
        # watchdog 频率要明显高于租约时长, 否则失联的 run 会长时间停在"正在回答"。
        cron(watchdog_tick, second={0, 20, 40}, run_at_startup=True),
        cron(memory_dispatch_tick, second={10, 30, 50}, run_at_startup=True),
        cron(cost_sweeper_tick, minute=set(range(0, 60, 5))),
    ]
    redis_settings = redis_settings()
    on_startup = startup
    on_shutdown = shutdown
    # 一个 run 由一个 worker 独占执行; 并发度控制的是同时跑几个 run。
    max_jobs = 4
    job_timeout = 600
    # 交给 claim_run 与 watchdog 判定重跑, arq 自身不重试, 避免重复计费。
    max_tries = 1
