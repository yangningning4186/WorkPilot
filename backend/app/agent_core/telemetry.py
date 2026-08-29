"""Small typed tracer shared by agent runtimes; storage remains an adapter concern."""

from __future__ import annotations

import time
from contextvars import Token
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import structlog

from workpilot_telemetry.spans import (
    AgentSpanAttributes,
    AgentSpanName,
    AgentSpanRecord,
    AgentSpanSink,
    AgentSpanStatus,
    bind_agent_span_id,
    current_agent_span_id,
    reset_agent_span_id,
)

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ActiveAgentSpan:
    span_id: str
    trace_id: str
    run_id: UUID
    parent_span_id: str | None
    name: AgentSpanName
    started_at: datetime
    started_monotonic: float
    token: Token[str | None]


class AgentTracer:
    def __init__(self, sink: AgentSpanSink | None, *, run_id: UUID, trace_id: str) -> None:
        self.sink = sink
        self.run_id = run_id
        self.trace_id = trace_id

    def start(
        self,
        name: AgentSpanName,
        *,
        parent_span_id: str | None = None,
    ) -> ActiveAgentSpan:
        span_id = uuid4().hex
        parent = parent_span_id or current_agent_span_id()
        return ActiveAgentSpan(
            span_id=span_id,
            trace_id=self.trace_id,
            run_id=self.run_id,
            parent_span_id=parent,
            name=name,
            started_at=datetime.now(UTC),
            started_monotonic=time.monotonic(),
            token=bind_agent_span_id(span_id),
        )

    async def finish(
        self,
        span: ActiveAgentSpan,
        *,
        status: AgentSpanStatus,
        attributes: AgentSpanAttributes,
        error: BaseException | None = None,
    ) -> None:
        reset_agent_span_id(span.token)
        if self.sink is None:
            return
        ended_at = datetime.now(UTC)
        record = AgentSpanRecord(
            span_id=span.span_id,
            trace_id=span.trace_id,
            run_id=span.run_id,
            parent_span_id=span.parent_span_id,
            name=span.name,
            status=status,
            started_at=span.started_at.isoformat(),
            ended_at=ended_at.isoformat(),
            duration_ms=max(0, round((time.monotonic() - span.started_monotonic) * 1000)),
            attributes=attributes,
            error_type=None if error is None else type(error).__name__,
        )
        try:
            await self.sink.record_span(record)
        except Exception as sink_error:  # pragma: no cover - observability must not break a run
            logger.warning(
                "agent.span_dropped",
                span_name=span.name,
                run_id=str(span.run_id),
                error=str(sink_error),
            )


__all__ = ["ActiveAgentSpan", "AgentTracer", "current_agent_span_id"]
