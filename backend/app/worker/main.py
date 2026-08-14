"""Arq worker 入口: uv run arq app.worker.main.WorkerSettings"""

from typing import Any, ClassVar

from arq import cron

from app.core.config import get_settings
from app.core.db import close_database, session_factory
from app.core.logging import configure_logging
from app.core.queue import redis_settings
from app.core.redis import close_redis, redis_client
from app.core.run_bus import RedisRunBus
from app.worker.answer_run import answer_run
from app.worker.maintenance import cost_sweeper_tick, watchdog_tick


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    ctx["settings"] = settings
    ctx["session_factory"] = session_factory
    ctx["bus"] = RedisRunBus(redis_client)


async def shutdown(ctx: dict[str, Any]) -> None:
    await close_redis()
    await close_database()


class WorkerSettings:
    functions: ClassVar = [answer_run]
    cron_jobs: ClassVar = [
        # watchdog 频率要明显高于租约时长, 否则失联的 run 会长时间停在"正在回答"。
        cron(watchdog_tick, second={0, 20, 40}, run_at_startup=True),
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
