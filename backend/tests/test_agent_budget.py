"""Agent run 三维预算与熔断（约束 5）。

这里证明的是"超限一定停"，不是"预算数字定得对"——上限本身是运营参数，不是被测行为。
"""

import asyncio

import pytest

from app.agent_core.budget import (
    BudgetedGateway,
    BudgetMeter,
    RunBudgetExceededError,
)
from tests.fakes import FrozenClock, review_budget
from workpilot_ai.gateway import ModelContextOverflowError
from workpilot_ai.types import (
    CompletionResult,
    EmbeddingResult,
    Message,
    ProviderNotDispatchedError,
    Usage,
)

_DEFAULT_USAGE = Usage(input_tokens=100, output_tokens=50)


class RecordingGateway:
    """按脚本回放的补全端；记录真正发出去的调用数，用来验证熔断发生在调用之前。"""

    def __init__(
        self,
        *,
        usage: Usage | None = None,
        error: Exception | None = None,
    ) -> None:
        self.usage = usage or _DEFAULT_USAGE
        self.error = error
        self.dispatched = 0

    async def complete(
        self,
        messages: list[Message],
        *,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> CompletionResult:
        self.dispatched += 1
        if self.error is not None:
            raise self.error
        return CompletionResult(text="回答", model="fake-chat", provider="fake", usage=self.usage)


class RecordingEmbeddingGateway(RecordingGateway):
    async def embed(
        self,
        texts: list[str],
        *,
        task_type: str = "embedding",
    ) -> EmbeddingResult:
        del texts, task_type
        self.dispatched += 1
        return EmbeddingResult(
            embeddings=[[0.1, 0.2]],
            model="fake-embedding",
            provider="fake",
            usage=Usage(input_tokens=2),
        )


class BlockingGateway(RecordingGateway):
    """让第一只请求保持飞行状态，用于检查未结算预留是否参与共享预算。"""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(
        self,
        messages: list[Message],
        *,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> CompletionResult:
        del messages, task_type, max_tokens, temperature
        self.dispatched += 1
        self.started.set()
        await self.release.wait()
        return CompletionResult(text="回答", model="fake-chat", provider="fake", usage=self.usage)


def _messages(chars: int = 40) -> list[Message]:
    return [Message(role="user", content="字" * chars)]


async def test_call_budget_trips_before_dispatching_the_next_call() -> None:
    meter = BudgetMeter(review_budget(max_calls=2), chars_per_token=1.0)
    gateway = RecordingGateway()
    budgeted = BudgetedGateway(gateway, meter)

    await budgeted.complete(_messages(), max_tokens=10)
    await budgeted.complete(_messages(), max_tokens=10)
    with pytest.raises(RunBudgetExceededError) as excinfo:
        await budgeted.complete(_messages(), max_tokens=10)

    # 第三次调用必须在发请求之前就被拦下, 否则熔断只是事后记账。
    assert gateway.dispatched == 2
    assert excinfo.value.dimension == "calls"
    assert meter.budget["used_calls"] == 2


async def test_concurrent_call_reservation_blocks_second_request_before_dispatch() -> None:
    meter = BudgetMeter(review_budget(max_calls=1), chars_per_token=1.0)
    gateway = BlockingGateway()
    budgeted = BudgetedGateway(gateway, meter)

    first = asyncio.create_task(budgeted.complete(_messages(), max_tokens=10))
    await gateway.started.wait()
    with pytest.raises(RunBudgetExceededError) as excinfo:
        await budgeted.complete(_messages(), max_tokens=10)

    assert excinfo.value.dimension == "calls"
    assert gateway.dispatched == 1
    # 第一只仍在飞行，尚未结算进 used_calls；第二只仍必须被共享预留挡住。
    assert meter.budget["used_calls"] == 0

    gateway.release.set()
    await first
    assert meter.budget["used_calls"] == 1


async def test_token_budget_reserves_worst_case_before_dispatching() -> None:
    # 输入估算 40 + max_tokens 60 = 100 的最坏情况预留, 正好用满 100 的上限。
    meter = BudgetMeter(review_budget(max_tokens=100), chars_per_token=1.0)
    gateway = RecordingGateway(usage=Usage(input_tokens=40, output_tokens=5))
    budgeted = BudgetedGateway(gateway, meter)

    await budgeted.complete(_messages(40), max_tokens=60)
    # 实际只用了 45, 结算按实际而不是预留, 否则一次保守估算会吃掉整个预算。
    assert meter.budget["used_tokens"] == 45

    with pytest.raises(RunBudgetExceededError) as excinfo:
        await budgeted.complete(_messages(40), max_tokens=60)
    assert excinfo.value.dimension == "tokens"
    assert gateway.dispatched == 1


async def test_wall_clock_budget_trips_without_any_model_call() -> None:
    clock = FrozenClock()
    meter = BudgetMeter(review_budget(max_wall_ms=1_000), clock=clock)

    clock.advance(999)
    meter.check_wall()

    clock.advance(2)
    with pytest.raises(RunBudgetExceededError) as excinfo:
        meter.check_wall()
    assert excinfo.value.dimension == "wall_ms"


async def test_zero_limits_keep_usage_without_stopping_a_long_run() -> None:
    """0 是不限制，不是一个已经耗尽的额度；三维计量仍必须保留。"""

    clock = FrozenClock()
    meter = BudgetMeter(
        review_budget(max_tokens=0, max_calls=0, max_wall_ms=0),
        chars_per_token=1.0,
        clock=clock,
    )
    gateway = RecordingGateway(usage=Usage(input_tokens=40, output_tokens=5))
    budgeted = BudgetedGateway(gateway, meter)

    clock.advance(24 * 60 * 60 * 1_000)
    meter.check_wall()
    for _ in range(100):
        await budgeted.complete(_messages(40), max_tokens=60)

    assert gateway.dispatched == 100
    assert meter.budget["used_calls"] == 100
    assert meter.budget["used_tokens"] == 4_500


async def test_wall_clock_excludes_time_between_execution_segments() -> None:
    """waiting_human 的人工思考时间与 worker 空档不计入墙钟预算。

    墙钟这一维防的是失控执行；把人的犹豫算进去只会让正常的 HITL 流程被误杀。
    """

    clock = FrozenClock()
    budget = review_budget(max_wall_ms=1_000)
    meter = BudgetMeter(budget, clock=clock)

    clock.advance(400)
    meter.settle_wall()
    assert budget["used_wall_ms"] == 400

    # 中间隔了 10 分钟人工确认, 之后新执行器接管已累计的墙钟。
    clock.advance(600_000)
    resumed = BudgetMeter(review_budget(max_wall_ms=1_000), clock=clock)
    resumed.adopt_wall(budget["used_wall_ms"])
    clock.advance(400)

    assert resumed.elapsed_ms() == 800
    resumed.check_wall()


async def test_undispatched_failure_is_not_charged_but_unknown_failure_is() -> None:
    """记账口径与网关的费用记账一致：只有能证明没发出去才免记。"""

    not_dispatched = BudgetMeter(review_budget(), chars_per_token=1.0)
    with pytest.raises(ProviderNotDispatchedError):
        await BudgetedGateway(
            RecordingGateway(error=ProviderNotDispatchedError("连接前失败")),
            not_dispatched,
        ).complete(_messages(40), max_tokens=60)
    assert not_dispatched.budget["used_calls"] == 0
    assert not_dispatched.budget["used_tokens"] == 0

    unknown = BudgetMeter(review_budget(), chars_per_token=1.0)
    with pytest.raises(TimeoutError):
        await BudgetedGateway(RecordingGateway(error=TimeoutError("读超时")), unknown).complete(
            _messages(40), max_tokens=60
        )
    # 请求可能已经打到模型上, 少记会让熔断失效, 因此按预留量保守记账。
    assert unknown.budget["used_calls"] == 1
    assert unknown.budget["used_tokens"] == 100


async def test_local_context_overflow_is_not_charged_as_a_dispatched_call() -> None:
    meter = BudgetMeter(review_budget(), chars_per_token=1.0)
    with pytest.raises(ModelContextOverflowError):
        await BudgetedGateway(
            RecordingGateway(error=ModelContextOverflowError("本地窗口预检失败")),
            meter,
        ).complete(_messages(40), max_tokens=60)

    assert meter.budget["used_calls"] == 0
    assert meter.budget["used_tokens"] == 0


async def test_embedding_for_rag_tool_uses_the_same_run_budget() -> None:
    meter = BudgetMeter(review_budget(max_tokens=10, max_calls=1), chars_per_token=1.0)
    gateway = RecordingEmbeddingGateway()

    result = await BudgetedGateway(gateway, meter).embed(["RAG!"])

    assert result.embeddings == [[0.1, 0.2]]
    assert gateway.dispatched == 1
    assert meter.budget["used_calls"] == 1
    assert meter.budget["used_tokens"] == 2
    with pytest.raises(RunBudgetExceededError):
        await BudgetedGateway(gateway, meter).embed(["again"])
    assert gateway.dispatched == 1
