"""Closed, provider-neutral span schema for agent execution trees."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypedDict
from uuid import UUID

from workpilot_telemetry.records import AttributeSpec

AgentSpanName = Literal["agent.run", "agent.turn", "agent.tool", "agent.compaction"]
AgentSpanStatus = Literal["ok", "error", "cancelled"]
_CURRENT_AGENT_SPAN_ID: ContextVar[str | None] = ContextVar(
    "workpilot_current_agent_span_id", default=None
)


def current_agent_span_id() -> str | None:
    return _CURRENT_AGENT_SPAN_ID.get()


def bind_agent_span_id(span_id: str) -> Token[str | None]:
    return _CURRENT_AGENT_SPAN_ID.set(span_id)


def reset_agent_span_id(token: Token[str | None]) -> None:
    _CURRENT_AGENT_SPAN_ID.reset(token)


class RunSpanAttributes(TypedDict):
    kind: Literal["run"]
    workflow: str
    status: str


class TurnSpanAttributes(TypedDict):
    kind: Literal["turn"]
    iteration: int
    stop_reason: str
    model: str
    provider: str


class ToolSpanAttributes(TypedDict):
    kind: Literal["tool"]
    tool: str
    tool_call_id: str
    step_idx: int
    status: str


class CompactionSpanAttributes(TypedDict):
    kind: Literal["compaction"]
    forced: bool
    changed: bool
    mode: str
    archived_messages: int
    before_tokens: int
    after_tokens: int
    trigger_source: str


AgentSpanAttributes = (
    RunSpanAttributes | TurnSpanAttributes | ToolSpanAttributes | CompactionSpanAttributes
)

SPAN_ATTRIBUTE_SCHEMAS: dict[AgentSpanName, dict[str, AttributeSpec]] = {
    "agent.run": {
        "kind": AttributeSpec((str,), True, "low", allowed_values=("run",)),
        "workflow": AttributeSpec((str,), True, "low"),
        "status": AttributeSpec((str,), True, "low"),
    },
    "agent.turn": {
        "kind": AttributeSpec((str,), True, "low", allowed_values=("turn",)),
        "iteration": AttributeSpec((int,), True, "medium"),
        "stop_reason": AttributeSpec((str,), True, "low"),
        "model": AttributeSpec((str,), True, "low"),
        "provider": AttributeSpec((str,), True, "low"),
    },
    "agent.tool": {
        "kind": AttributeSpec((str,), True, "low", allowed_values=("tool",)),
        "tool": AttributeSpec((str,), True, "low"),
        "tool_call_id": AttributeSpec((str,), True, "high"),
        "step_idx": AttributeSpec((int,), True, "medium"),
        "status": AttributeSpec((str,), True, "low"),
    },
    "agent.compaction": {
        "kind": AttributeSpec((str,), True, "low", allowed_values=("compaction",)),
        "forced": AttributeSpec((bool,), True, "low"),
        "changed": AttributeSpec((bool,), True, "low"),
        "mode": AttributeSpec(
            (str,),
            True,
            "low",
            allowed_values=("none", "summary", "summary_fallback", "trim", "error"),
        ),
        "archived_messages": AttributeSpec((int,), True, "medium"),
        "before_tokens": AttributeSpec((int,), True, "medium"),
        "after_tokens": AttributeSpec((int,), True, "medium"),
        "trigger_source": AttributeSpec(
            (str,),
            True,
            "low",
            allowed_values=("provider_usage", "estimate", "unknown"),
        ),
    },
}


def validate_span_attributes(name: AgentSpanName, attributes: AgentSpanAttributes) -> None:
    schema = SPAN_ATTRIBUTE_SCHEMAS[name]
    values = dict(attributes)
    if set(values) != set(schema):
        raise ValueError(
            f"{name} attribute schema 漂移: "
            f"missing={sorted(set(schema) - set(values))}, "
            f"extra={sorted(set(values) - set(schema))}"
        )
    for key, spec in schema.items():
        value: Any = values[key]
        if not isinstance(value, spec.python_types):
            raise TypeError(f"{name}.{key} 类型错误: {type(value).__name__}")
        if spec.allowed_values and value not in spec.allowed_values:
            raise ValueError(f"{name}.{key} 值不在封闭集合中: {value!r}")


@dataclass(frozen=True)
class AgentSpanRecord:
    span_id: str
    trace_id: str
    run_id: UUID
    parent_span_id: str | None
    name: AgentSpanName
    status: AgentSpanStatus
    started_at: str
    ended_at: str
    duration_ms: int
    attributes: AgentSpanAttributes
    error_type: str | None = None


class AgentSpanSink(Protocol):
    async def record_span(self, span: AgentSpanRecord) -> None: ...


__all__ = [
    "SPAN_ATTRIBUTE_SCHEMAS",
    "AgentSpanAttributes",
    "AgentSpanName",
    "AgentSpanRecord",
    "AgentSpanSink",
    "AgentSpanStatus",
    "CompactionSpanAttributes",
    "RunSpanAttributes",
    "ToolSpanAttributes",
    "TurnSpanAttributes",
    "bind_agent_span_id",
    "current_agent_span_id",
    "reset_agent_span_id",
    "validate_span_attributes",
]
