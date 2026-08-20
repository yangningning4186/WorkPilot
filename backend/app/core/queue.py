"""任务队列入口。

worker 不依附 HTTP 连接: 接口只负责创建 run 并入队, 执行、落库、发事件全部在
worker 进程完成。这样关掉页面任务照跑, 刷新回放与实时流共用同一份 run_events。
"""

import asyncio
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.core.config import get_settings

ANSWER_RUN_TASK = "answer_run"
REVIEW_RUN_TASK = "review_run"
COWORK_RUN_TASK = "cowork_run"
MEMORY_EXTRACTION_TASK = "memory_extraction_job"
SKILL_DISTILLATION_TASK = "skill_distillation_job"


class RunQueue(Protocol):
    async def enqueue_answer_run(self, run_id: UUID, *, top_k: int) -> None: ...

    async def enqueue_review_run(self, run_id: UUID, *, attempt: int = 0) -> None: ...

    async def enqueue_cowork_run(self, run_id: UUID, *, attempt: int = 0) -> None: ...

    async def enqueue_memory_job(self, job_id: UUID, *, attempt: int = 0) -> None: ...

    async def enqueue_skill_job(self, job_id: UUID, *, attempt: int = 0) -> None: ...


QueueTaskName = Literal[
    "answer_run",
    "review_run",
    "cowork_run",
    "memory_extraction_job",
    "skill_distillation_job",
]


@dataclass(frozen=True)
class QueuedTask:
    name: QueueTaskName
    object_id: UUID
    attempt: int = 0
    top_k: int = 5


class InProcessRunQueue:
    """桌面单体运行时的低延迟唤醒队列。

    队列不是持久化真相：进程退出时尚未消费的项目允许丢失，dispatcher 会根据
    SQLite/PostgreSQL 中的 queued 状态重新发现。集合只用于减少同进程重复唤醒。
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[QueuedTask] = asyncio.Queue()
        self._pending: set[tuple[QueueTaskName, UUID]] = set()
        self._closed = False

    async def _put(self, task: QueuedTask) -> None:
        if self._closed:
            raise RuntimeError("本地任务队列已关闭")
        key = (task.name, task.object_id)
        if key in self._pending:
            return
        self._pending.add(key)
        await self._queue.put(task)

    async def enqueue_answer_run(self, run_id: UUID, *, top_k: int) -> None:
        await self._put(QueuedTask("answer_run", run_id, top_k=top_k))

    async def enqueue_review_run(self, run_id: UUID, *, attempt: int = 0) -> None:
        await self._put(QueuedTask("review_run", run_id, attempt=attempt))

    async def enqueue_cowork_run(self, run_id: UUID, *, attempt: int = 0) -> None:
        await self._put(QueuedTask("cowork_run", run_id, attempt=attempt))

    async def enqueue_memory_job(self, job_id: UUID, *, attempt: int = 0) -> None:
        await self._put(QueuedTask("memory_extraction_job", job_id, attempt=attempt))

    async def enqueue_skill_job(self, job_id: UUID, *, attempt: int = 0) -> None:
        await self._put(QueuedTask("skill_distillation_job", job_id, attempt=attempt))

    async def get(self) -> QueuedTask:
        return await self._queue.get()

    def task_done(self, task: QueuedTask) -> None:
        self._pending.discard((task.name, task.object_id))
        self._queue.task_done()

    async def close(self) -> None:
        self._closed = True
        self._pending.clear()


class ArqRunQueue:
    def __init__(self, pool: ArqRedis) -> None:
        self._pool = pool

    async def enqueue_answer_run(self, run_id: UUID, *, top_k: int) -> None:
        # _job_id 用 run_id: 队列重投递不会产生第二个执行体; 真正的双跑防线仍是
        # claim_run 的条件 UPDATE, 这里只是少一次无谓唤醒。
        await self._pool.enqueue_job(
            ANSWER_RUN_TASK,
            str(run_id),
            top_k,
            _job_id=f"{ANSWER_RUN_TASK}:{run_id}",
        )

    async def enqueue_review_run(self, run_id: UUID, *, attempt: int = 0) -> None:
        # watchdog 重投必须换一个 job_id: arq 按 job_id 去重, 被 SIGKILL 的那个作业
        # 在 result/in-progress 键过期前一直占着原 id, 沿用会被静默丢弃, 恢复就永远不发生。
        # 双跑防线仍然是 claim_run 的条件 UPDATE, 换 id 不会放松它。
        job_id = f"{REVIEW_RUN_TASK}:{run_id}"
        if attempt > 0:
            job_id = f"{job_id}:r{attempt}"
        await self._pool.enqueue_job(REVIEW_RUN_TASK, str(run_id), _job_id=job_id)

    async def enqueue_cowork_run(self, run_id: UUID, *, attempt: int = 0) -> None:
        job_id = f"{COWORK_RUN_TASK}:{run_id}"
        if attempt > 0:
            job_id = f"{job_id}:r{attempt}"
        await self._pool.enqueue_job(COWORK_RUN_TASK, str(run_id), _job_id=job_id)

    async def enqueue_memory_job(self, job_id: UUID, *, attempt: int = 0) -> None:
        # 作业失败回到 queued 后必须换 job_id，否则 arq 的旧 result key 会把重投静默去重。
        await self._pool.enqueue_job(
            MEMORY_EXTRACTION_TASK,
            str(job_id),
            _job_id=f"{MEMORY_EXTRACTION_TASK}:{job_id}:r{attempt}",
        )

    async def enqueue_skill_job(self, job_id: UUID, *, attempt: int = 0) -> None:
        # 不固定 Redis job_id：滚动升级时旧 worker 可能先消费并以“未知函数”失败；若沿用
        # r0，arq 的 result key 会让 DB dispatcher 的补偿入队被静默去重。真正的并发防线
        # 是 claim_skill_job 的条件 UPDATE 与租约，因此重复唤醒安全。
        await self._pool.enqueue_job(
            SKILL_DISTILLATION_TASK,
            str(job_id),
        )


_pool: ArqRedis | None = None
_in_process_queue: InProcessRunQueue | None = None


def redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(get_settings().redis_url)


async def get_run_queue() -> RunQueue:
    global _in_process_queue, _pool
    if get_settings().task_queue_backend == "in_process":
        if _in_process_queue is None:
            _in_process_queue = InProcessRunQueue()
        return _in_process_queue
    if _pool is None:
        _pool = await create_pool(redis_settings())
    return ArqRunQueue(_pool)


async def get_in_process_run_queue() -> InProcessRunQueue:
    queue = await get_run_queue()
    if not isinstance(queue, InProcessRunQueue):
        raise RuntimeError("当前未启用进程内任务队列")
    return queue


async def close_run_queue() -> None:
    global _in_process_queue, _pool
    if _in_process_queue is not None:
        await _in_process_queue.close()
        _in_process_queue = None
    if _pool is not None:
        await _pool.aclose()
        _pool = None
