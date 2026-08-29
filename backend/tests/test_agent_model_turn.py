import pytest

from app.agent_core.budget import RunBudgetExceededError
from app.agent_core.model_turn import run_model_turn
from workpilot_ai.errors import (
    ModelContextOverflowError,
    ProviderResponseError,
    ProviderRouteTimeoutError,
)
from workpilot_ai.types import CompletionResult


async def test_model_turn_returns_completion_without_throwing() -> None:
    completion = CompletionResult(text="ok", model="fake", provider="fake")

    outcome = await run_model_turn(_return(completion))

    assert outcome.stop_reason == "complete"
    assert outcome.completion is completion
    assert outcome.error is None


async def test_model_turn_preserves_length_as_truncated_terminal() -> None:
    completion = CompletionResult(
        text="half",
        model="fake",
        provider="fake",
        stop_reason="length",
    )

    outcome = await run_model_turn(_return(completion))

    assert outcome.stop_reason == "truncated"
    assert outcome.completion is completion


@pytest.mark.parametrize(
    ("error", "stop_reason"),
    [
        (ModelContextOverflowError("too long"), "context_overflow"),
        (
            RunBudgetExceededError("calls", used=2, limit=1),
            "budget_exceeded",
        ),
        (ProviderRouteTimeoutError("timeout"), "retryable_error"),
        (ProviderResponseError("bad response"), "error"),
    ],
)
async def test_model_turn_encodes_normalized_failures(
    error: Exception,
    stop_reason: str,
) -> None:
    outcome = await run_model_turn(_raise(error))

    assert outcome.stop_reason == stop_reason
    assert outcome.completion is None
    assert outcome.error is error


async def test_model_turn_does_not_hide_programming_errors() -> None:
    with pytest.raises(RuntimeError, match="bug"):
        await run_model_turn(_raise(RuntimeError("bug")))


async def _return(completion: CompletionResult) -> CompletionResult:
    return completion


async def _raise(error: Exception) -> CompletionResult:
    raise error
