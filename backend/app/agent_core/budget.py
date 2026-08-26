"""Agent run 的 token / 调用数 / 墙钟计量与可选熔断。

与 `app.telemetry.cost_budget` 的每日金额上限是两件不同的事：那一条护的是钱包总量，
这一层首先负责持续记账。部署方可把任一上限设为正数以启用对应熔断；0 表示不限制。
桌面 Cowork 默认不启用单次 run 上限，长任务依靠用户取消、重复调用刹车、上下文压缩
与每日费用保护收敛，而不是因为累计工作量较大被判成失败。

计量口径：
- **调用数**与**墙钟**在调用前精确判定，不会超出上限；
- **token** 在调用前按最坏情况（输入估算 + `max_tokens`）预留，调用后按实际结算。
  预留偏大是刻意的，与 `llm/pricing.estimate_tokens` 的理由一致：估小了会穿透上限。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from typing import Literal, Protocol, cast

from app.agent_core.contracts import BudgetState
from workpilot_ai.errors import (
    ModelContextOverflowError,
    ProviderContextOverflowError,
    ProviderNotDispatchedError,
)
from workpilot_ai.gateway import PromptBudget, request_character_count
from workpilot_ai.pricing import estimate_tokens
from workpilot_ai.types import (
    CompletionChunk,
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


class StreamingToolCompletionClient(Protocol):
    def stream_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool = True,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> AsyncIterator[CompletionChunk]: ...


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
    """三维计量器；直接读写 Agent 状态的 budget 子结构，随 checkpoint 一起落盘。"""

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
        # `reserve` 到 `settle` 之间可能同时挂着多个 explore 模型请求。事件循环里预留本身
        # 没有 await，因此这两项足以成为原子账本；不能只看已结算量，否则并发分支会一起
        # 通过调用前检查，事后才发现共享预算已经穿透。
        self._reserved_calls = 0
        self._reserved_tokens = 0

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
        """检查可选墙钟上限。0 表示只计量、不熔断。"""

        elapsed = self.elapsed_ms()
        limit = self._budget["max_wall_ms"]
        if limit > 0 and elapsed > limit:
            raise RunBudgetExceededError("wall_ms", used=elapsed, limit=self._budget["max_wall_ms"])

    def reserve(self, *, projected_tokens: int) -> None:
        """调用前预留。任一维度超限立即抛，且抛之前不记任何消耗。"""

        self.check_wall()
        call_limit = self._budget["max_calls"]
        projected_calls = self._budget["used_calls"] + self._reserved_calls + 1
        if call_limit > 0 and projected_calls > call_limit:
            raise RunBudgetExceededError("calls", used=projected_calls, limit=call_limit)
        projected = self._budget["used_tokens"] + self._reserved_tokens + projected_tokens
        token_limit = self._budget["max_tokens"]
        if token_limit > 0 and projected > token_limit:
            raise RunBudgetExceededError("tokens", used=projected, limit=token_limit)
        self._reserved_calls += 1
        self._reserved_tokens += projected_tokens

    def release(self, *, projected_tokens: int) -> None:
        """释放一个可证明未发出的请求预留，不产生实际用量。"""

        self._release_reservation(projected_tokens=projected_tokens)

    def settle(self, usage: Usage, *, projected_tokens: int) -> None:
        """调用成功后按实际用量结算。"""

        self._release_reservation(projected_tokens=projected_tokens)
        self._budget["used_calls"] += 1
        self._budget["used_tokens"] += usage.input_tokens + usage.output_tokens

    def settle_conservative(self, *, projected_tokens: int) -> None:
        """请求已发出但结果不可知时的保守记账。

        与网关的费用记账同口径：只要不能证明"一个字节都没发出去"，就按预留量记账。
        少记会让熔断失效，多记只是让这个 run 早一点停。
        """

        self._release_reservation(projected_tokens=projected_tokens)
        self._budget["used_calls"] += 1
        self._budget["used_tokens"] += projected_tokens

    def settle_rejected(self, *, projected_tokens: int) -> None:
        """服务端明确因上下文过长拒绝：记一次调用，但没有生成 token 用量。"""

        self._release_reservation(projected_tokens=projected_tokens)
        self._budget["used_calls"] += 1

    def _release_reservation(self, *, projected_tokens: int) -> None:
        if self._reserved_calls <= 0 or self._reserved_tokens < projected_tokens:
            raise RuntimeError("模型预算预留账本不一致")
        self._reserved_calls -= 1
        self._reserved_tokens -= projected_tokens

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
            self._meter.release(projected_tokens=projected)
            raise
        except ProviderContextOverflowError:
            self._meter.settle_rejected(projected_tokens=projected)
            raise
        except asyncio.CancelledError:
            self._meter.settle_conservative(projected_tokens=projected)
            raise
        except Exception:
            self._meter.settle_conservative(projected_tokens=projected)
            raise
        self._meter.settle(result.usage, projected_tokens=projected)
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
            self._meter.release(projected_tokens=projected)
            raise
        except ProviderContextOverflowError:
            self._meter.settle_rejected(projected_tokens=projected)
            raise
        except asyncio.CancelledError:
            self._meter.settle_conservative(projected_tokens=projected)
            raise
        except Exception:
            self._meter.settle_conservative(projected_tokens=projected)
            raise
        self._meter.settle(result.usage, projected_tokens=projected)
        return result

    async def stream_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool = True,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> AsyncIterator[CompletionChunk]:
        """`complete_with_tools` 的流式版本，计量口径逐字相同。

        **预留在第一块之前、结算在终块之后**，和非流式一样：中途按已产出的 delta 边流边
        记账听起来更精确，实际上会让同一次调用在预算里出现好几次，恢复与重放时对不上。

        失败路径也保持一致——包括"流已经吐了一半才断"这种非流式没有的情形：它同样按
        保守估算结算，因为那些 token 已经产生、已经计费。
        """

        gateway = cast("StreamingToolCompletionClient", self._gateway)
        projected = self._meter.project_tokens(messages, max_tokens=max_tokens, tools=tools)
        self._meter.reserve(projected_tokens=projected)
        settled = False
        try:
            async for chunk in gateway.stream_with_tools(
                messages,
                tools=tools,
                parallel_tool_calls=parallel_tool_calls,
                task_type=task_type,
                max_tokens=max_tokens,
                temperature=temperature,
            ):
                if chunk.result is not None:
                    self._meter.settle(chunk.result.usage, projected_tokens=projected)
                    settled = True
                yield chunk
        except (ProviderNotDispatchedError, ModelContextOverflowError):
            if not settled:
                self._meter.release(projected_tokens=projected)
            raise
        except ProviderContextOverflowError:
            if not settled:
                self._meter.settle_rejected(projected_tokens=projected)
            raise
        except asyncio.CancelledError:
            if not settled:
                self._meter.settle_conservative(projected_tokens=projected)
            raise
        except Exception:
            if not settled:
                self._meter.settle_conservative(projected_tokens=projected)
            raise
        if not settled:
            # 流正常结束却没有终块：网关的契约要求终块存在，走到这里说明有实现违约了。
            # 不记账地放过去会让这一轮凭空免费, 所以按保守估算落账再抛。
            self._meter.settle_conservative(projected_tokens=projected)
            raise RuntimeError("流式补全没有给出终块，无法确定这一轮的结果与用量")

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
            self._meter.release(projected_tokens=projected)
            raise
        except ProviderContextOverflowError:
            self._meter.settle_rejected(projected_tokens=projected)
            raise
        except asyncio.CancelledError:
            self._meter.settle_conservative(projected_tokens=projected)
            raise
        except Exception:
            self._meter.settle_conservative(projected_tokens=projected)
            raise
        self._meter.settle(result.usage, projected_tokens=projected)
        return result
