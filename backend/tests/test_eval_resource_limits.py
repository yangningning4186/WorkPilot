from __future__ import annotations

import asyncio

import pytest

from eval.cowork_runner import _EvaluationMeteredGateway
from eval.resource_limits import (
    EvaluationBudget,
    EvaluationLimitExceeded,
    EvaluationLimits,
)
from workpilot_ai.gateway import PromptBudget
from workpilot_ai.types import CompletionResult, Message, Usage


class _UsageFreeGateway:
    chat_provider = "fixture"
    chat_model = "fixture"
    embedding_provider = "fixture"
    embedding_model = "fixture"
    embedding_dimensions = 1

    def __init__(self) -> None:
        self.calls = 0

    def prompt_budget(self, task_type: str, *, max_tokens: int) -> PromptBudget:
        return PromptBudget(
            task_type=task_type,
            tier="main",
            model="fixture",
            context_window_tokens=128,
            max_output_tokens=max_tokens,
            safety_tokens=0,
        )

    async def complete(self, *args, **kwargs) -> CompletionResult:
        self.calls += 1
        return CompletionResult(
            text="ok",
            model="fixture",
            provider="fixture",
            usage=Usage(),
        )

    async def stream(self, *args, **kwargs):
        self.calls += 1
        yield "first"
        yield "second"


@pytest.mark.asyncio
async def test_concurrent_token_reservations_trip_before_second_model_dispatch() -> None:
    budget = EvaluationBudget(
        EvaluationLimits(max_total_tokens=10, max_model_calls=2, max_wall_seconds=60)
    )

    first = await budget.reserve_model_call(projected_tokens=6)
    with pytest.raises(EvaluationLimitExceeded, match="total_tokens"):
        await budget.reserve_model_call(projected_tokens=5)

    await budget.settle_model_call(first, actual_tokens=4)
    second = await budget.reserve_model_call(projected_tokens=6)
    await budget.settle_model_call(second, actual_tokens=5)
    assert await budget.snapshot() == {
        "model_calls": 2,
        "total_tokens": 9,
        "reserved_tokens": 0,
        "conservative_settlements": 0,
        "wall_seconds": pytest.approx(0.0, abs=0.1),
        "status": "within_limits",
    }


@pytest.mark.asyncio
async def test_unknown_provider_usage_is_charged_at_reserved_ceiling() -> None:
    budget = EvaluationBudget(
        EvaluationLimits(max_total_tokens=8, max_model_calls=1, max_wall_seconds=60)
    )
    reservation = await budget.reserve_model_call(projected_tokens=8)

    await budget.settle_model_call(reservation, actual_tokens=None)

    usage = await budget.snapshot()
    assert usage["total_tokens"] == 8
    assert usage["conservative_settlements"] == 1
    with pytest.raises(EvaluationLimitExceeded, match="model_calls"):
        await budget.reserve_model_call(projected_tokens=1)


@pytest.mark.asyncio
async def test_a_reservation_cannot_be_settled_twice_or_consume_a_peer() -> None:
    budget = EvaluationBudget(
        EvaluationLimits(max_total_tokens=20, max_model_calls=2, max_wall_seconds=60)
    )
    first = await budget.reserve_model_call(projected_tokens=5)
    second = await budget.reserve_model_call(projected_tokens=5)

    await budget.settle_model_call(first, actual_tokens=2)
    with pytest.raises(RuntimeError, match="already settled"):
        await budget.settle_model_call(first, actual_tokens=2)

    usage = await budget.snapshot()
    assert usage["reserved_tokens"] == 5
    await budget.settle_model_call(second, actual_tokens=3)
    assert (await budget.snapshot())["total_tokens"] == 5


@pytest.mark.asyncio
async def test_wall_fuse_refuses_work_after_deadline() -> None:
    budget = EvaluationBudget(
        EvaluationLimits(max_total_tokens=100, max_model_calls=2, max_wall_seconds=0.01)
    )
    await asyncio.sleep(0.02)

    with pytest.raises(EvaluationLimitExceeded, match="wall_seconds"):
        await budget.reserve_model_call(projected_tokens=1)


@pytest.mark.parametrize(
    ("tokens", "calls", "wall"),
    [(0, 1, 1), (1, 0, 1), (1, 1, 0)],
)
def test_resource_limits_never_accept_disabled_or_unbounded_values(
    tokens: int, calls: int, wall: int
) -> None:
    with pytest.raises(ValueError):
        EvaluationLimits(
            max_total_tokens=tokens,
            max_model_calls=calls,
            max_wall_seconds=wall,
        )


@pytest.mark.asyncio
async def test_cowork_eval_gateway_blocks_before_dispatch_and_charges_missing_usage() -> None:
    delegate = _UsageFreeGateway()
    budget = EvaluationBudget(
        EvaluationLimits(max_total_tokens=128, max_model_calls=1, max_wall_seconds=60)
    )
    gateway = _EvaluationMeteredGateway(delegate, budget)  # type: ignore[arg-type]

    await gateway.complete([Message(role="user", content="x")], max_tokens=5)

    assert delegate.calls == 1
    usage = await budget.snapshot()
    assert usage["total_tokens"] == 128
    with pytest.raises(EvaluationLimitExceeded, match="model_calls"):
        await gateway.complete([Message(role="user", content="x")], max_tokens=1)
    assert delegate.calls == 1


@pytest.mark.asyncio
async def test_cowork_eval_gateway_token_fuse_trips_before_first_dispatch() -> None:
    delegate = _UsageFreeGateway()
    gateway = _EvaluationMeteredGateway(  # type: ignore[arg-type]
        delegate,
        EvaluationBudget(
            EvaluationLimits(max_total_tokens=5, max_model_calls=1, max_wall_seconds=60)
        ),
    )

    with pytest.raises(EvaluationLimitExceeded, match="total_tokens"):
        await gateway.complete([Message(role="user", content="x")], max_tokens=5)
    assert delegate.calls == 0


@pytest.mark.asyncio
async def test_closing_a_model_stream_early_conservatively_settles_its_reservation() -> None:
    delegate = _UsageFreeGateway()
    budget = EvaluationBudget(
        EvaluationLimits(max_total_tokens=128, max_model_calls=1, max_wall_seconds=60)
    )
    gateway = _EvaluationMeteredGateway(delegate, budget)  # type: ignore[arg-type]
    stream = gateway.stream([Message(role="user", content="x")], max_tokens=5)

    assert await anext(stream) == "first"
    await stream.aclose()

    usage = await budget.snapshot()
    assert usage["reserved_tokens"] == 0
    assert usage["total_tokens"] == 128
    assert usage["conservative_settlements"] == 1
