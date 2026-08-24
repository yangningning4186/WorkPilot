"""run 事件写入器: 文本增量定时批量落库, 结构化事件立即落库。"""

import asyncio
from typing import Any

import structlog

from app.core.db import SessionFactory
from app.core.run_bus import RunBus
from app.runstore.runs import RunEvent, append_events

logger = structlog.get_logger(__name__)


class RunEventEmitter:
    """按 N 字或 N 毫秒合并 delta 写入。

    逐条写 delta 会把写入量放大一到两个数量级(ADR-0007 代价 3)。代价是 worker
    进程崩溃时最多丢一个未提交批次——此时消息会被 watchdog 标为 failed 显式暴露,
    而不是伪装成一条完整回答。
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        bus: RunBus,
        *,
        run_id: Any,
        flush_interval_s: float,
        flush_chars: int,
    ) -> None:
        self._session_factory = session_factory
        self._bus = bus
        self._run_id = run_id
        self._flush_interval_s = flush_interval_s
        self._flush_chars = flush_chars
        # 正文和 reasoning 都要批量，但不能混成同一种事件。用一条
        # 有序队列保留两路偶尔交错时的真实顺序，相邻同类增量再合并。
        self._pending: list[tuple[str, str]] = []
        self._pending_chars = 0
        self._lock = asyncio.Lock()
        self._timer_task: asyncio.Task[None] | None = None

    async def delta(self, text: str) -> None:
        await self._buffer("message.delta", text)

    async def reasoning(self, text: str) -> None:
        await self._buffer("message.reasoning", text)

    async def _buffer(self, event_type: str, text: str) -> None:
        if not text:
            return
        async with self._lock:
            if self._pending and self._pending[-1][0] == event_type:
                previous_type, previous_text = self._pending[-1]
                self._pending[-1] = (previous_type, previous_text + text)
            else:
                self._pending.append((event_type, text))
            self._pending_chars += len(text)
            if self._timer_task is None:
                self._timer_task = asyncio.create_task(self._flush_after_interval())
            flush_now = self._pending_chars >= self._flush_chars
        if flush_now:
            await self.flush()

    async def emit(self, event_type: str, payload: dict[str, Any]) -> list[RunEvent]:
        """写一个结构化事件。

        先 flush 待发 delta, 否则 message.done 会排在它总结的正文前面, 前端按 seq
        重放就会看到"先结束后正文"。
        """

        async with self._lock:
            self._cancel_timer_locked()
            events = self._pending_events()
            events.append((event_type, payload))
            written = await self._write(events)
            self._clear_pending_locked()
            return written

    async def flush(self) -> None:
        """立即刷出当前批次。

        定时器、字符阈值和结构化事件都可能同时触发 flush；锁一直
        持有到事务提交完，才能保证 seq 不把后来的 reset/done 排到正文前面。
        """

        async with self._lock:
            self._cancel_timer_locked()
            if not self._pending:
                return
            await self._write(self._pending_events())
            self._clear_pending_locked()

    async def drain(self) -> None:
        """每轮模型流结束时的显式排空点。"""

        await self.flush()

    async def _flush_after_interval(self) -> None:
        try:
            await asyncio.sleep(self._flush_interval_s)
            await self.flush()
        except asyncio.CancelledError:
            return
        except Exception:
            # 定时任务没有直接 await 它的调用者，失败必须显式记录。
            # pending 只在成功提交后清空，下一个增量或 drain 仍可重试。
            logger.exception("run delta 定时 flush 失败", run_id=str(self._run_id))

    def _cancel_timer_locked(self) -> None:
        task = self._timer_task
        self._timer_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _pending_events(self) -> list[tuple[str, dict[str, Any]]]:
        return [(event_type, {"text": text}) for event_type, text in self._pending]

    def _clear_pending_locked(self) -> None:
        self._pending.clear()
        self._pending_chars = 0

    async def _write(self, events: list[tuple[str, dict[str, Any]]]) -> list[RunEvent]:
        async with self._session_factory() as session:
            written = await append_events(session, run_id=self._run_id, events=events)
            await session.commit()
        # 提交之后才通知: 反过来订阅方会被唤醒却查不到事件, 白跑一轮。
        await self._bus.publish(self._run_id)
        return written
