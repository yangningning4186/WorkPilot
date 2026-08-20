"""Agent run 的 token / 调用数 / 墙钟三维预算与熔断（约束 5）。

与 `app.services.cost_budget` 的每日金额上限是两件不同的事：那一条护的是钱包总量，
这一条护的是**单个 run 不许失控**——固定综述的抽卡节点按文档数循环调用模型，
将来接反思循环后更是天然会自我放大。两者都触发时先撞到哪条就报哪条。

计量口径：
- **调用数**与**墙钟**在调用前精确判定，不会超出上限；
- **token** 在调用前按最坏情况（输入估算 + `max_tokens`）预留，调用后按实际结算。
  预留偏大是刻意的，与 `llm/pricing.estimate_tokens` 的理由一致：估小了会穿透上限。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Literal, Protocol, cast

from app.agent_core.contracts import BudgetState
from app.llm.errors import (
    ModelContextOverflowError,
    ProviderContextOverflowError,
    ProviderNotDispatchedError,
)
from app.llm.gateway import PromptBudget, request_character_count
from app.llm.pricing import estimate_tokens
from app.llm.types import (
    CompletionResult,
    EmbeddingResult,
    Message,
    ToolDefinition,
    Usage,
)

BudgetDimension = Literal["tokens", "calls", "wall_ms"]

_DIMENSION_LABELS: dict[BudgetDimension, str] = {
    "tokens": "token",
    "calls": "模型调用次数",
    "wall_ms": "执行墙钟(ms)",
}


class RunBudgetExceededError(RuntimeError):
    """单个 run 的预算熔断。

    **不可重试**：重跑同一节点只会继续消耗同一份预算，重试三次只是把超额放大三倍。
    调用方必须让 run 落 `budget_exceeded` 终态，由人决定是否提额后重开。
    """

    def __init__(self, dimension: BudgetDimension, *, used: int, limit: int) -> None:
        self.dimension = dimension
        self.used = used
        self.limit = limit
        super().__init__(
            f"run 预算熔断：{_DIMENSION_LABELS[dimension]} 已用 {used}，上限 {limit}。"
            "该 run 不会自动重试，请确认是否提高预算后重新发起。"
        )


class CompletionClient(Protocol):
    """工具层依赖的最小补全接口。

    真实实现始终是包在 `ModelGateway` 外面的 `BudgetedGateway`，业务代码拿不到裸 SDK，
    也拿不到未计量的网关（约束 1）。
    """

    async def complete(
        self,
        messages: list[Message],
        *,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> CompletionResult: ...


class ToolCompletionClient(Protocol):
    async def complete_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool = True,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> CompletionResult: ...


class EmbeddingClient(Protocol):
    async def embed(
        self,
        texts: list[str],
        *,
        task_type: str = "embedding",
    ) -> EmbeddingResult: ...


class PromptBudgetClient(Protocol):
    def prompt_budget(
        self,
        task_type: str,
        *,
        max_tokens: int,
    ) -> PromptBudget: ...


class BudgetMeter:
    """三维计量器；直接读写 `AgentState` 的 budget 子结构，随 checkpoint 一起落盘。"""

    def __init__(
        self,
        budget: BudgetState,
        *,
        chars_per_token: float = 1.0,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._budget = budget
        self._chars_per_token = chars_per_token
        self._clock = clock or (lambda: int(time.monotonic() * 1000))
        self._segment_started_ms = self._clock()

    @property
    def budget(self) -> BudgetState:
        return self._budget

    def adopt_wall(self, used_wall_ms: int) -> None:
        """接管 checkpoint 里已累计的执行墙钟，并重新开始分段。

        只接管墙钟这一维，是因为它是**唯一**只存在于 checkpoint 的量：上限与
        token/调用数消耗都以 `agent_runs` 行为准。若连上限也从 checkpoint 抄回来，
        运维给某个 run 提额之后恢复执行会继续用旧上限，提额等于没生效。
        """

        if used_wall_ms < 0:
            raise ValueError("已耗墙钟不能为负")
        self._budget["used_wall_ms"] = used_wall_ms
        self._segment_started_ms = self._clock()

    def elapsed_ms(self) -> int:
        """已累计的执行墙钟 = 此前分段之和 + 当前分段已过时长。"""

        return self._budget["used_wall_ms"] + max(0, self._clock() - self._segment_started_ms)

    def settle_wall(self) -> None:
        """把当前分段结算进 `used_wall_ms` 并开启新分段；落 checkpoint 前调用。"""

        now = self._clock()
        self._budget["used_wall_ms"] += max(0, now - self._segment_started_ms)
        self._segment_started_ms = now

    def check_wall(self) -> None:
        """只查墙钟。用在不产生模型调用、但可能长时间执行的节点入口。"""

        elapsed = self.elapsed_ms()
        if elapsed > self._budget["max_wall_ms"]:
            raise RunBudgetExceededError("wall_ms", used=elapsed, limit=self._budget["max_wall_ms"])

    def reserve(self, *, projected_tokens: int) -> None:
        """调用前预留。任一维度超限立即抛，且抛之前不记任何消耗。"""

        self.check_wall()
        if self._budget["used_calls"] + 1 > self._budget["max_calls"]:
            raise RunBudgetExceededError(
                "calls", used=self._budget["used_calls"] + 1, limit=self._budget["max_calls"]
            )
        projected = self._budget["used_tokens"] + projected_tokens
        if projected > self._budget["max_tokens"]:
            raise RunBudgetExceededError("tokens", used=projected, limit=self._budget["max_tokens"])

    def settle(self, usage: Usage) -> None:
        """调用成功后按实际用量结算。"""

        self._budget["used_calls"] += 1
        self._budget["used_tokens"] += usage.input_tokens + usage.output_tokens

    def settle_conservative(self, *, projected_tokens: int) -> None:
        """请求已发出但结果不可知时的保守记账。

        与网关的费用记账同口径：只要不能证明"一个字节都没发出去"，就按预留量记账。
        少记会让熔断失效，多记只是让这个 run 早一点停。
        """

        self._budget["used_calls"] += 1
        self._budget["used_tokens"] += projected_tokens

    def settle_rejected(self) -> None:
        """服务端明确因上下文过长拒绝：记一次调用，但没有生成 token 用量。"""

        self._budget["used_calls"] += 1

    def project_tokens(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        tools: list[ToolDefinition] | None = None,
    ) -> int:
        return (
            estimate_tokens(
                request_character_count(messages, tools),
                chars_per_token=self._chars_per_token,
            )
            + max_tokens
        )

    def project_embedding_tokens(self, texts: list[str]) -> int:
        if not texts or any(not text.strip() for text in texts):
            raise ValueError("embedding 输入不能为空")
        return estimate_tokens(
            sum(len(text) for text in texts),
            chars_per_token=self._chars_per_token,
        )


class BudgetedGateway:
    """在统一模型网关之上叠一层 per-run 计量；不绕过网关（约束 1）。"""

    def __init__(self, gateway: CompletionClient, meter: BudgetMeter) -> None:
        self._gateway = gateway
        self._meter = meter

    def prompt_budget(self, task_type: str, *, max_tokens: int) -> PromptBudget:
        gateway = cast("PromptBudgetClient", self._gateway)
        return gateway.prompt_budget(task_type, max_tokens=max_tokens)

    async def complete(
        self,
        messages: list[Message],
        *,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> CompletionResult:
        projected = self._meter.project_tokens(messages, max_tokens=max_tokens)
        self._meter.reserve(projected_tokens=projected)
        try:
            result = await self._gateway.complete(
                messages,
                task_type=task_type,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except (ProviderNotDispatchedError, ModelContextOverflowError):
            # 能证明请求没发出去, 不记账。这是唯一允许不记账的失败。
            raise
        except ProviderContextOverflowError:
            self._meter.settle_rejected()
            raise
        except Exception:
            self._meter.settle_conservative(projected_tokens=projected)
            raise
        self._meter.settle(result.usage)
        return result

    async def complete_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool = True,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> CompletionResult:
        gateway = cast("ToolCompletionClient", self._gateway)
        projected = self._meter.project_tokens(messages, max_tokens=max_tokens, tools=tools)
        self._meter.reserve(projected_tokens=projected)
        try:
            result = await gateway.complete_with_tools(
                messages,
                tools=tools,
                parallel_tool_calls=parallel_tool_calls,
                task_type=task_type,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except (ProviderNotDispatchedError, ModelContextOverflowError):
            raise
        except ProviderContextOverflowError:
            self._meter.settle_rejected()
            raise
        except Exception:
            self._meter.settle_conservative(projected_tokens=projected)
            raise
        self._meter.settle(result.usage)
        return result

    async def embed(
        self,
        texts: list[str],
        *,
        task_type: str = "embedding",
    ) -> EmbeddingResult:
        """让检索工具继续经过统一网关，并计入当前 Agent run 的预算。"""

        projected = self._meter.project_embedding_tokens(texts)
        self._meter.reserve(projected_tokens=projected)
        gateway = cast("EmbeddingClient", self._gateway)
        try:
            result = await gateway.embed(texts, task_type=task_type)
        except (ProviderNotDispatchedError, ModelContextOverflowError):
            raise
        except ProviderContextOverflowError:
            self._meter.settle_rejected()
            raise
        except Exception:
            self._meter.settle_conservative(projected_tokens=projected)
            raise
        self._meter.settle(result.usage)
        return result
