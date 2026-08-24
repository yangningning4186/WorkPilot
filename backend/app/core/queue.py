"""任务队列入口。

worker 不依附 HTTP 连接: 接口只负责创建 run 并入队, 执行、落库、发事件全部在
worker 里完成。这样关掉页面任务照跑, 刷新回放与实时流共用同一份 run_events。

**只有进程内一种实现。** 原来还有一条 Arq/Redis 的路, 用于 API 与 worker 分进程
部署; 桌面单用户应用里那条路从来没被用过, 却逼着整套东西依赖一个外部 broker。
持久化真相在 SQLite 的 queued 状态里, 队列只是低延迟唤醒——这一点两种实现本来
就一致(见 InProcessRunQueue 的说明), 所以删掉的只是传输方式。
"""

import asyncio
from dataclasses import dataclass
from itertools import count
from typing import Literal, Protocol
from uuid import UUID


class RunQueue(Protocol):
    async def enqueue_cowork_run(self, run_id: UUID, *, attempt: int = 0) -> None: ...

    async def enqueue_memory_job(self, job_id: UUID, *, attempt: int = 0) -> None: ...

    async def enqueue_skill_job(self, run_id: UUID, *, attempt: int = 0) -> None: ...


QueueTaskName = Literal[
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
    SQLite 中的 queued 状态重新发现。集合只用于减少同进程重复唤醒。

    用户 Cowork run 和后台后处理物理分队列。仅仅给一个统一 PriorityQueue 标优先级
    不够：4 个 consumer 可能已经全部卡在慢蒸馏请求里，新来的用户任务仍然没有槽位。
    运行时因此为 foreground/background 配独立 consumer；这里负责保存这条边界。
    """

    def __init__(self) -> None:
        self._foreground_queue: asyncio.Queue[QueuedTask] = asyncio.Queue()
        # 记忆抽取比 Skill 蒸馏更接近用户可见状态，后台内部也显式分优先级。
        self._background_queue: asyncio.PriorityQueue[tuple[int, int, QueuedTask]] = (
            asyncio.PriorityQueue()
        )
        self._sequence = count()
        self._pending: set[tuple[QueueTaskName, UUID]] = set()
        self._closed = False

    async def _put(self, task: QueuedTask) -> None:
        if self._closed:
            raise RuntimeError("本地任务队列已关闭")
        key = (task.name, task.object_id)
        if key in self._pending:
            return
        self._pending.add(key)
        if task.name == "cowork_run":
            await self._foreground_queue.put(task)
            return
        priority = 0 if task.name == "memory_extraction_job" else 10
        await self._background_queue.put((priority, next(self._sequence), task))

    async def enqueue_cowork_run(self, run_id: UUID, *, attempt: int = 0) -> None:
        await self._put(QueuedTask("cowork_run", run_id, attempt=attempt))

    async def enqueue_memory_job(self, job_id: UUID, *, attempt: int = 0) -> None:
        await self._put(QueuedTask("memory_extraction_job", job_id, attempt=attempt))

    async def enqueue_skill_job(self, run_id: UUID, *, attempt: int = 0) -> None:
        await self._put(QueuedTask("skill_distillation_job", run_id, attempt=attempt))

    async def get(self) -> QueuedTask:
        """兼容已有直接队列用例；运行时应使用两个显式消费入口。"""

        if not self._foreground_queue.empty():
            return await self.get_foreground()
        return await self.get_background()

    async def get_foreground(self) -> QueuedTask:
        return await self._foreground_queue.get()

    async def get_background(self) -> QueuedTask:
        _, _, task = await self._background_queue.get()
        return task

    def has_foreground_work(self) -> bool:
        return not self._foreground_queue.empty()

    def task_done(self, task: QueuedTask) -> None:
        self._pending.discard((task.name, task.object_id))
        if task.name == "cowork_run":
            self._foreground_queue.task_done()
        else:
            self._background_queue.task_done()

    async def close(self) -> None:
        self._closed = True
        self._pending.clear()


_in_process_queue: InProcessRunQueue | None = None


async def get_run_queue() -> RunQueue:
    global _in_process_queue
    if _in_process_queue is None:
        _in_process_queue = InProcessRunQueue()
    return _in_process_queue


async def get_in_process_run_queue() -> InProcessRunQueue:
    queue = await get_run_queue()
    if not isinstance(queue, InProcessRunQueue):
        raise RuntimeError("当前未启用进程内任务队列")
    return queue


async def close_run_queue() -> None:
    global _in_process_queue
    if _in_process_queue is not None:
        await _in_process_queue.close()
        _in_process_queue = None
