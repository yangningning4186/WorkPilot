"""桌面单体运行时：进程内低延迟队列 + 持久化状态轮询补偿。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from app.core.config import Settings
from app.core.db import session_factory
from app.core.queue import InProcessRunQueue, QueuedTask, get_in_process_run_queue
from app.core.run_bus import in_memory_run_bus
from app.cowork.browser_tools import PlaywrightBrowserManager
from app.cowork.mcp.client import McpClientManager
from app.cowork.shell_sessions import CoworkPersistentShellManager
from app.cowork_store.factory import local_cowork_stores
from app.worker.cowork_run import cowork_run
from app.worker.maintenance import (
    cost_sweeper_tick,
    memory_dispatch_tick,
    next_run_dispatch_tick,
    scheduler_dispatch_tick,
    skill_distillation_dispatch_tick,
    team_wake_dispatch_tick,
    watchdog_tick,
)
from app.worker.memory_run import memory_extraction_job
from app.worker.skill_distillation_run import skill_distillation_job

logger = structlog.get_logger(__name__)
Tick = Callable[[dict[str, Any]], Awaitable[int]]


class EmbeddedWorkerRuntime:
    """与 FastAPI 同进程的 worker。

    内存队列只降低提交延迟；数据库中的 queued/job 状态才是恢复真相。每个轮询周期
    都会重新发现未消费任务，因此 API 在 enqueue 前崩溃或进程退出都不会永久丢任务。
    """

    def __init__(self, settings: Settings, queue: InProcessRunQueue) -> None:
        self.settings = settings
        self.queue = queue
        self.ctx: dict[str, Any] = {
            "settings": settings,
            "session_factory": session_factory,
            "bus": in_memory_run_bus(),
            "run_queue": queue,
        }
        self._tasks: list[asyncio.Task[None]] = []
        self._active_foreground = 0

    @classmethod
    async def start(cls, settings: Settings) -> EmbeddedWorkerRuntime:
        runtime = cls(settings, await get_in_process_run_queue())
        runtime._tasks.extend(
            asyncio.create_task(runtime._consume_foreground(), name=f"embedded-foreground-{index}")
            for index in range(4)
        )
        # 后处理只有一个专用槽位。它不会占用四个用户任务 consumer，并且开始执行前
        # 还要经过 foreground idle 门控。
        runtime._tasks.append(
            asyncio.create_task(runtime._consume_background(), name="embedded-background")
        )
        runtime._tasks.append(
            asyncio.create_task(runtime._dispatch_queued_runs(), name="embedded-run-dispatcher")
        )
        runtime._tasks.extend(
            [
                asyncio.create_task(
                    runtime._periodic(watchdog_tick, 20.0), name="embedded-watchdog"
                ),
                asyncio.create_task(
                    runtime._periodic(memory_dispatch_tick, 20.0), name="embedded-memory"
                ),
                asyncio.create_task(
                    runtime._periodic(skill_distillation_dispatch_tick, 20.0),
                    name="embedded-skill-distillation",
                ),
                asyncio.create_task(
                    runtime._periodic(scheduler_dispatch_tick, 15.0), name="embedded-scheduler"
                ),
                asyncio.create_task(
                    runtime._periodic(team_wake_dispatch_tick, 1.0),
                    name="embedded-team-wake",
                ),
                asyncio.create_task(
                    runtime._periodic(next_run_dispatch_tick, 1.0),
                    name="embedded-next-run",
                ),
                asyncio.create_task(
                    runtime._periodic(cost_sweeper_tick, 300.0), name="embedded-cost-sweeper"
                ),
            ]
        )
        logger.info(
            "桌面嵌入式 worker 已启动",
            foreground_concurrency=4,
            background_concurrency=1,
        )
        return runtime

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        browser_manager = self.ctx.get("browser_manager")
        if isinstance(browser_manager, PlaywrightBrowserManager):
            await browser_manager.aclose()
        cached = self.ctx.get("mcp_manager_cache")
        if isinstance(cached, dict):
            managers = {
                manager for manager in cached.values() if isinstance(manager, McpClientManager)
            }
            await asyncio.gather(*(manager.aclose() for manager in managers))
        shell_sessions = self.ctx.get("shell_session_manager")
        if isinstance(shell_sessions, CoworkPersistentShellManager):
            await shell_sessions.aclose()
        logger.info("桌面嵌入式 worker 已停止")

    async def _consume_foreground(self) -> None:
        while True:
            task = await self.queue.get_foreground()
            self._active_foreground += 1
            try:
                await self._execute(task)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 各执行体负责把已 claim 的状态收敛为可恢复/终态；这里不做无条件重试，
                # 避免模型请求或外部副作用重复执行。
                logger.exception(
                    "嵌入式前台 worker 任务异常",
                    task=task.name,
                    object_id=str(task.object_id),
                )
            finally:
                self._active_foreground -= 1
                self.queue.task_done(task)

    async def _consume_background(self) -> None:
        while True:
            task = await self.queue.get_background()
            try:
                await self._wait_for_foreground_idle()
                await self._execute(task)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "嵌入式后台 worker 任务异常",
                    task=task.name,
                    object_id=str(task.object_id),
                )
            finally:
                self.queue.task_done(task)

    async def _wait_for_foreground_idle(self) -> None:
        """后台作业只能从真正空闲的前台边界开始。

        内存队列之外还查一次 SQLite：API 可能已经把 run 持久化、dispatcher 尚未来得及
        放进内存队列。后台后处理宁可晚 100ms，也不能钻这个窗口抢在用户任务前面。
        """

        poll_s = min(self.settings.cowork_dispatch_poll_s, 0.1)
        while True:
            queued_runs = await local_cowork_stores().state.list_queued_runs(limit=1)
            if (
                self._active_foreground == 0
                and not self.queue.has_foreground_work()
                and not queued_runs
            ):
                return
            await asyncio.sleep(poll_s)

    async def _execute(self, task: QueuedTask) -> None:
        raw_id = str(task.object_id)
        if task.name == "cowork_run":
            await cowork_run(self.ctx, raw_id)
        elif task.name == "memory_extraction_job":
            await memory_extraction_job(self.ctx, raw_id)
        elif task.name == "skill_distillation_job":
            await skill_distillation_job(self.ctx, raw_id)
        else:  # pragma: no cover - QueueTaskName 已封闭
            raise ValueError(f"未知的本地任务类型: {task.name}")

    async def _dispatch_queued_runs(self) -> None:
        while True:
            try:
                for run in await local_cowork_stores().state.list_queued_runs(limit=100):
                    await self.queue.enqueue_cowork_run(run.id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("嵌入式 run dispatcher 扫描失败")
            await asyncio.sleep(self.settings.cowork_dispatch_poll_s)

    async def _periodic(self, tick: Tick, interval_s: float) -> None:
        while True:
            try:
                await tick(self.ctx)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("嵌入式维护任务失败", tick=tick.__name__)
            await asyncio.sleep(interval_s)
