import copy

from app.agent.budget import BudgetedGateway, BudgetMeter
from app.agent.cowork_compaction import (
    CoworkOutboundCompactor,
    default_compaction_state,
)
from app.llm.gateway import ModelGateway
from app.llm.types import ToolDefinition
from tests.fakes import DeterministicProvider, review_budget


def _round(call_id: str, content: str) -> list[dict[str, object]]:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "inspect", "arguments": '{"path":"a"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": content},
    ]


def _compactor(
    provider: DeterministicProvider,
    *,
    keep_recent: int,
    trigger_ratio: float = 0.4,
) -> tuple[CoworkOutboundCompactor, BudgetMeter]:
    gateway = ModelGateway(
        provider,
        embedding_dimensions=4,
        default_context_window_tokens=3_000,
        context_safety_tokens=0,
    )
    meter = BudgetMeter(review_budget(max_tokens=50_000, max_calls=20))
    budgeted = BudgetedGateway(gateway, meter)
    compactor = CoworkOutboundCompactor(
        budgeted,
        tools=[
            ToolDefinition(
                name="inspect",
                description="inspect",
                parameters={"type": "object", "properties": {}},
            )
        ],
        system_prompt="system",
        enabled=True,
        trigger_ratio=trigger_ratio,
        keep_recent_tool_rounds=keep_recent,
        max_summary_chars=400,
        max_input_chars=2_000,
        max_tokens=100,
        decision_max_tokens=200,
    )
    return compactor, meter


async def test_compaction_only_changes_outbound_view_and_keeps_canonical_suffix() -> None:
    provider = DeterministicProvider(
        completion_text='{"summary":"前两次 inspect 已完成，保留了关键结果。"}'
    )
    compactor, meter = _compactor(provider, keep_recent=1)
    canonical: list[dict[str, object]] = [{"role": "user", "content": "检查三个文件"}]
    canonical += _round("call-1", "一" * 500)
    canonical += _round("call-2", "二" * 500)
    canonical += _round("call-3", "三" * 500)
    original = copy.deepcopy(canonical)

    prepared = await compactor.prepare(canonical, default_compaction_state(), forced=False)

    assert canonical == original
    assert prepared.changed is True
    assert prepared.compaction["summary_upto"] == 5
    assert prepared.after_tokens < prepared.before_tokens
    assert meter.budget["used_calls"] == 1
    outbound = prepared.messages
    assert outbound[1].content == "检查三个文件"
    assert "cowork_history_summary" in outbound[2].content
    assert all(
        call.id not in {"call-1", "call-2"} for message in outbound for call in message.tool_calls
    )
    assert outbound[-2].tool_calls[0].id == "call-3"
    assert outbound[-1].tool_call_id == "call-3"


async def test_compaction_retries_summary_once_then_uses_unattended_trim_fallback() -> None:
    provider = DeterministicProvider(completion_texts=["not json", "still not json"])
    compactor, meter = _compactor(provider, keep_recent=2, trigger_ratio=1.0)
    canonical: list[dict[str, object]] = [{"role": "user", "content": "检查文件"}]
    canonical += _round("call-1", '{"ok":true,"effect_ref":"file:a"}')

    prepared = await compactor.prepare(canonical, default_compaction_state(), forced=True)

    assert prepared.changed is True
    assert prepared.mode == "summary_fallback"
    assert "inspect[call-1]" in prepared.compaction["summary"]
    assert "effect_ref" in prepared.compaction["summary"]
    assert meter.budget["used_calls"] == 2
