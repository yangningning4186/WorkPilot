"""任务队列入口。

worker 不依附 HTTP 连接: 接口只负责创建 run 并入队, 执行、落库、发事件全部在
worker 进程完成。这样关掉页面任务照跑, 刷新回放与实时流共用同一份 run_events。
"""

from typing import Protocol
from uuid import UUID

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.core.config import get_settings

ANSWER_RUN_TASK = "answer_run"
REVIEW_RUN_TASK = "review_run"
MEMORY_EXTRACTION_TASK = "memory_extraction_job"


class RunQueue(Protocol):
    async def enqueue_answer_run(self, run_id: UUID, *, top_k: int) -> None: ...

    async def enqueue_review_run(self, run_id: UUID, *, attempt: int = 0) -> None: ...

    async def enqueue_memory_job(self, job_id: UUID, *, attempt: int = 0) -> None: ...


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

    async def enqueue_memory_job(self, job_id: UUID, *, attempt: int = 0) -> None:
        # 作业失败回到 queued 后必须换 job_id，否则 arq 的旧 result key 会把重投静默去重。
        await self._pool.enqueue_job(
            MEMORY_EXTRACTION_TASK,
            str(job_id),
            _job_id=f"{MEMORY_EXTRACTION_TASK}:{job_id}:r{attempt}",
        )


_pool: ArqRedis | None = None


def redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(get_settings().redis_url)


async def get_run_queue() -> RunQueue:
    global _pool
    if _pool is None:
        _pool = await create_pool(redis_settings())
    return ArqRunQueue(_pool)


async def close_run_queue() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None
