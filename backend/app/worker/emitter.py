"""run 事件写入器: delta 批量落库, 结构化事件逐条落库。"""

from time import monotonic
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.run_bus import RunBus
from app.services.runs import RunEvent, append_events

logger = structlog.get_logger(__name__)


class RunEventEmitter:
    """按 N 字或 N 毫秒合并 delta 写入。

    逐条写 delta 会把写入量放大一到两个数量级(ADR-0007 代价 3)。代价是 worker
    进程崩溃时最多丢一个未提交批次——此时消息会被 watchdog 标为 failed 显式暴露,
    而不是伪装成一条完整回答。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
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
        self._pending: list[str] = []
        self._pending_chars = 0
        self._last_flush = monotonic()

    async def delta(self, text: str) -> None:
        if not text:
            return
        self._pending.append(text)
        self._pending_chars += len(text)
        if (
            self._pending_chars >= self._flush_chars
            or monotonic() - self._last_flush >= self._flush_interval_s
        ):
            await self.flush()

    async def emit(self, event_type: str, payload: dict[str, Any]) -> list[RunEvent]:
        """写一个结构化事件。

        先 flush 待发 delta, 否则 message.done 会排在它总结的正文前面, 前端按 seq
        重放就会看到"先结束后正文"。
        """

        await self.flush()
        return await self._write([(event_type, payload)])

    async def flush(self) -> None:
        if not self._pending:
            return
        text = "".join(self._pending)
        self._pending.clear()
        self._pending_chars = 0
        self._last_flush = monotonic()
        await self._write([("message.delta", {"text": text})])

    async def _write(self, events: list[tuple[str, dict[str, Any]]]) -> list[RunEvent]:
        async with self._session_factory() as session:
            written = await append_events(session, run_id=self._run_id, events=events)
            await session.commit()
        # 提交之后才通知: 反过来订阅方会被唤醒却查不到事件, 白跑一轮。
        await self._bus.publish(self._run_id)
        return written
