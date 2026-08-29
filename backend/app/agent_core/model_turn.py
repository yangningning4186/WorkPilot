"""把模型调用的规范失败编码成回合结果，避免 loop 用异常分支表达控制流。"""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Literal

from app.agent_core.budget import RunBudgetExceededError
from workpilot_ai.errors import (
    ModelContextOverflowError,
    ProviderContextOverflowError,
    ProviderError,
    ProviderRouteTimeoutError,
)
from workpilot_ai.types import CompletionResult

ModelTurnStopReason = Literal[
    "complete",
    "truncated",
    "context_overflow",
    "budget_exceeded",
    "retryable_error",
    "error",
]


@dataclass(frozen=True)
class ModelTurnResult:
    stop_reason: ModelTurnStopReason
    completion: CompletionResult | None = None
    error: Exception | None = None


async def run_model_turn(
    invocation: Awaitable[CompletionResult],
) -> ModelTurnResult:
    """执行一次补全；规范化模型失败不越过这条边界。

    ``asyncio.CancelledError`` 与编程错误仍会传播：前者是任务取消协议，后者不应被
    伪装成 provider 失败。Provider adapter 有义务把请求失败归一成 ProviderError。
    """

    try:
        completion = await invocation
    except RunBudgetExceededError as error:
        return ModelTurnResult("budget_exceeded", error=error)
    except (ModelContextOverflowError, ProviderContextOverflowError) as error:
        return ModelTurnResult("context_overflow", error=error)
    except ProviderRouteTimeoutError as error:
        return ModelTurnResult("retryable_error", error=error)
    except ProviderError as error:
        return ModelTurnResult("error", error=error)
    if completion.stop_reason == "length":
        return ModelTurnResult("truncated", completion=completion)
    if completion.stop_reason == "error":
        return ModelTurnResult(
            "error",
            completion=completion,
            error=ProviderError("模型以错误终止原因结束生成"),
        )
    return ModelTurnResult("complete", completion=completion)
