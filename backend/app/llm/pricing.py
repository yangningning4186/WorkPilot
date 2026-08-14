"""模型价格与 token 估算；纯函数，不依赖数据库。"""

from dataclasses import dataclass, field
from decimal import ROUND_UP, Decimal
from math import ceil

from app.llm.types import Usage

# 与 cost_reservations.estimated_usd / llm_calls.cost_usd 的 NUMERIC(12,6) 对齐。
COST_QUANTUM = Decimal("0.000001")
_PER_MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class ModelPricing:
    """每百万 token 单价。本地自部署模型填 0, 预留与结算随之短路。"""

    input_usd_per_mtok: Decimal = Decimal(0)
    output_usd_per_mtok: Decimal = Decimal(0)

    @property
    def is_free(self) -> bool:
        return self.input_usd_per_mtok == 0 and self.output_usd_per_mtok == 0

    def cost_usd(self, usage: Usage) -> Decimal:
        """估算与结算共用同一函数, 且一律向上取整。

        两端同向取整才能保证 "token 数不增 ⇒ 费用不增", 否则结算可能因为进位
        超过预留上限, 被 settle_cost 的区间校验拒绝。
        """

        raw = (
            Decimal(usage.input_tokens) * self.input_usd_per_mtok
            + Decimal(usage.output_tokens) * self.output_usd_per_mtok
        ) / _PER_MILLION
        return raw.quantize(COST_QUANTUM, rounding=ROUND_UP)


@dataclass(frozen=True)
class GatewayPricing:
    """网关当前档位的价格表。M0 只有 main 一档, 路由分档见 M2。"""

    chat: ModelPricing = field(default_factory=ModelPricing)
    embedding: ModelPricing = field(default_factory=ModelPricing)

    @property
    def is_free(self) -> bool:
        return self.chat.is_free and self.embedding.is_free


def estimate_tokens(chars: int, *, chars_per_token: float) -> int:
    """按字符数保守估算 token 数。

    默认 1 字符 = 1 token 是上界而不是均值: 预留必须偏大, 结算时多余部分会释放,
    估小了则会穿透每日上限。
    """

    if chars < 0:
        raise ValueError("字符数不能为负数")
    if chars_per_token <= 0:
        raise ValueError("chars_per_token 必须大于 0")
    return ceil(chars / chars_per_token)


def is_measured(usage: Usage) -> bool:
    """provider 是否真的回报了用量。

    部分 OpenAI-compatible 服务不返回 usage 字段; 这种情况下不能当作 0 计费,
    否则等于免费用掉了预留额度。
    """

    return usage.input_tokens > 0 or usage.output_tokens > 0
