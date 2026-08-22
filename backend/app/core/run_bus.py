"""run 事件的唤醒通道。

只负责"有新事件了, 去查库", 不传输事件内容。通知可以丢: 订阅方每次醒来和心跳
超时都会重新查库, 数据库才是真相源(ADR-0007)。把事件内容塞进 pub/sub 会让在线
推送和历史回放变成两条路径, 那正是要避免的不一致来源。

**只有进程内一种实现。** Redis pub/sub 那条路是给"worker 与 SSE 分进程"准备的;
API 与 worker 同进程之后没有跨进程可唤醒, 而"通知可以丢"这条前提让它删起来没有
代价——真被丢了, 心跳超时那一轮照样查得到库里的新事件。
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol
from uuid import UUID


class RunSubscription(Protocol):
    async def wait(self, timeout_s: float) -> bool:
        """等待唤醒; 超时返回 False。返回值只用于日志与心跳, 不影响正确性。"""
        ...


class RunBus(Protocol):
    async def publish(self, run_id: UUID) -> None: ...

    def subscribe(self, run_id: UUID) -> AbstractAsyncContextManager[RunSubscription]: ...


def run_channel(run_id: UUID) -> str:
    return f"run-events:{run_id}"


class _InMemorySubscription:
    def __init__(self, event: asyncio.Event) -> None:
        self._event = event

    async def wait(self, timeout_s: float) -> bool:
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout_s)
        except TimeoutError:
            return False
        self._event.clear()
        return True


class InMemoryRunBus:
    """单进程实现, 用于测试与本地单体运行。"""

    def __init__(self) -> None:
        self._subscribers: dict[UUID, list[asyncio.Event]] = {}

    async def publish(self, run_id: UUID) -> None:
        for event in self._subscribers.get(run_id, []):
            event.set()

    @asynccontextmanager
    async def subscribe(self, run_id: UUID) -> AsyncIterator[RunSubscription]:
        event = asyncio.Event()
        self._subscribers.setdefault(run_id, []).append(event)
        try:
            yield _InMemorySubscription(event)
        finally:
            listeners = self._subscribers.get(run_id, [])
            if event in listeners:
                listeners.remove(event)
            if not listeners:
                self._subscribers.pop(run_id, None)


_in_memory_run_bus = InMemoryRunBus()


def in_memory_run_bus() -> InMemoryRunBus:
    """API 与嵌入式 worker 共进程时共享同一个唤醒总线。"""

    return _in_memory_run_bus
