"""置信度驱动的动态升档（docs/07 §3）。

与 fallback 是两件事，别混：

| | 触发条件 | 语义 |
|---|---|---|
| fallback | 调用**失败**（连不上、超时、协议错） | 换一条路把同一件事做完 |
| 升档 | 调用**成功但结果不可信** | 同一件事换个更强的模型重做 |

置信度判定必须是**免费的确定性检查**——schema 校验、关键字段是否为空、
逐字引用是否对得上。用另一次模型调用去判断上一次调用可不可信，
既贵又会把两个模型的错误叠在一起。

`run_with_escalation` 只负责"跑一轮 → 不达标就换档再跑一轮"这个骨架；
一轮内部要不要 repair、怎么 repair，是调用方自己的事（那属于 prompt 策略，
不属于路由）。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog

from workpilot_ai.routing import Tier

logger = structlog.get_logger(__name__)


class EscalationRejected(Exception):
    """本轮输出没通过置信度判定，请求升档。

    调用方在自己的校验失败时抛这个；带上 reason 是为了让升档率的归因
    能落到具体原因上（schema_invalid / empty_field / quote_mismatch …），
    而不是只知道"升了 37%"。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class EscalationAttempt:
    tier: Tier | None
    reason: str


@dataclass(frozen=True)
class EscalationOutcome[T]:
    value: T
    tier: Tier | None
    escalated: bool
    # 被拒掉的那些轮次。升档率与归因直接从这里统计（§3 要测量的第一个数）。
    rejected: tuple[EscalationAttempt, ...]


async def run_with_escalation[T](
    run: Callable[[Tier | None], Awaitable[T]],
    *,
    task_type: str,
    start_tier: Tier | None,
    escalate_to: Tier | None,
) -> EscalationOutcome[T]:
    """先在 `start_tier` 跑一轮；`run` 抛 EscalationRejected 就换 `escalate_to` 重跑。

    只升一档。链式升档（light → main → heavy）看着更完备，实际上会把
    "这个任务本来就答不了"的情况变成三倍成本——§3 要量的是升档划不划算，
    不是把失败拖得更贵。

    `escalate_to` 为 None（没配升档目标，或目标不比起始档高）时，
    EscalationRejected 直接上抛，行为与不接这套机制时一致。
    """

    try:
        value = await run(start_tier)
    except EscalationRejected as rejection:
        if escalate_to is None:
            raise
        logger.info(
            "置信度不达标, 升档重做",
            task_type=task_type,
            from_tier=start_tier,
            to_tier=escalate_to,
            reason=rejection.reason,
        )
        # 升档后再被拒就直接抛出去: 更强的模型也答不了, 说明问题不在档位上。
        upgraded = await run(escalate_to)
        return EscalationOutcome(
            value=upgraded,
            tier=escalate_to,
            escalated=True,
            rejected=(EscalationAttempt(tier=start_tier, reason=rejection.reason),),
        )
    return EscalationOutcome(value=value, tier=start_tier, escalated=False, rejected=())
