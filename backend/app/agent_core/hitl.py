"""Human-in-the-loop 暂停与恢复的通用纯函数。"""

from __future__ import annotations

from typing import Any

from app.agent_core.contracts import HumanInterrupt


class HumanInterruptMismatchError(ValueError):
    """恢复请求不属于当前 checkpoint 中等待的人工中断。"""


def build_human_interrupt(
    *,
    inbox_id: object,
    kind: str,
    resume_token: object,
    tool_call_id: str,
    step_id: object,
    step_idx: int,
    request: dict[str, Any],
) -> HumanInterrupt:
    if not kind or not tool_call_id or step_idx < 0:
        raise ValueError("HITL interrupt 缺少 kind/tool_call_id 或 step_idx 非法")
    return {
        "inbox_id": str(inbox_id),
        "kind": kind,
        "resume_token": str(resume_token),
        "tool_call_id": tool_call_id,
        "step_id": str(step_id),
        "step_idx": step_idx,
        "request": request,
    }


def interrupt_event_payload(interrupt: HumanInterrupt) -> dict[str, Any]:
    return {
        "inbox_id": interrupt["inbox_id"],
        "kind": interrupt["kind"],
        "resume_token": interrupt["resume_token"],
        "payload": interrupt["request"],
    }


def validate_human_resume(
    interrupt: HumanInterrupt,
    *,
    resume_token: object,
    tool_call_id: str,
) -> None:
    if (
        interrupt["resume_token"] != str(resume_token)
        or interrupt["tool_call_id"] != tool_call_id
    ):
        raise HumanInterruptMismatchError("resume token 与当前 checkpoint 不匹配")
