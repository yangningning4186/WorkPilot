"""run 事件的唤醒通道。

只负责"有新事件了, 去查库", 不传输事件内容。通知可以丢: 订阅方每次醒来和心跳
超时都会重新查库, 数据库才是真相源(ADR-0007)。把事件内容塞进 pub/sub 会让在线
推送和历史回放变成两条路径, 那正是要避免的不一致来源。
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol
from uuid import UUID

from redis.asyncio import Redis
from redis.asyncio.client import PubSub


class RunSubscription(Protocol):
    async def wait(self, timeout_s: float) -> bool:
        """等待唤醒; 超时返回 False。返回值只用于日志与心跳, 不影响正确性。"""
        ...


class RunBus(Protocol):
    async def publish(self, run_id: UUID) -> None: ...

    def subscribe(self, run_id: UUID) -> AbstractAsyncContextManager[RunSubscription]: ...


def run_channel(run_id: UUID) -> str:
    return f"run-events:{run_id}"


class _RedisSubscription:
    def __init__(self, pubsub: PubSub) -> None:
        self._pubsub = pubsub

    async def wait(self, timeout_s: float) -> bool:
        message = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout_s)
        return message is not None


class RedisRunBus:
    """跨进程唤醒: worker 在 worker 进程, SSE 在 web 进程。"""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def publish(self, run_id: UUID) -> None:
        await self._redis.publish(run_channel(run_id), "1")

    @asynccontextmanager
    async def subscribe(self, run_id: UUID) -> AsyncIterator[RunSubscription]:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(run_channel(run_id))
        try:
            yield _RedisSubscription(pubsub)
        finally:
            await pubsub.aclose()  # type: ignore[no-untyped-call]


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
