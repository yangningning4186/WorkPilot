"""BudgetGuard / AuditSink 实现的一致性检查。

不是 pytest 用例，是一组**可被任何适配器复用**的断言函数。PostgreSQL 实现、
桌面 SQLite 实现、测试用的假实现，都应该能原样跑过同一套。

放在包里而不是 `backend/tests/`：契约的验证方式属于契约本身。一个 `BudgetGuard`
只有跑过这里才算实现了这个 Protocol——Protocol 的方法签名管不住"预留没结算就不该扣钱"
这类语义。

用法::

    from workpilot_telemetry.conformance import assert_budget_guard_conforms
    await assert_budget_guard_conforms(guard, spent=my_spent_reader)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal

from workpilot_telemetry.budget import BudgetGuard
from workpilot_telemetry.records import AuditRecord, AuditSink

# 读取"当前已计入的金额"。每个适配器的存法不同，因此由调用方提供。
SpentReader = Callable[[], Awaitable[Decimal]]


class ConformanceError(AssertionError):
    """实现违反了 Protocol 的语义约定（而不是签名）。"""


async def assert_budget_guard_conforms(
    guard: BudgetGuard,
    *,
    spent: SpentReader,
    key_prefix: str = "conformance",
) -> None:
    """逐条验证费用闸门必须满足的四条语义。"""

    await _same_key_reserves_once(guard, spent=spent, key=f"{key_prefix}-idempotent")
    await _release_undoes_reservation(guard, spent=spent, key=f"{key_prefix}-release")
    await _settle_replaces_estimate(guard, spent=spent, key=f"{key_prefix}-settle")
    await _release_after_settle_is_noop(guard, spent=spent, key=f"{key_prefix}-noop")


async def _same_key_reserves_once(guard: BudgetGuard, *, spent: SpentReader, key: str) -> None:
    """同一 idempotency_key 重复预留只应扣一次。

    网关重试、worker 复活都会用同一个键再来一次。这条不成立就会重复计费。
    """

    before = await spent()
    await guard.reserve(idempotency_key=key, estimated_usd=Decimal("0.10"))
    await guard.reserve(idempotency_key=key, estimated_usd=Decimal("0.10"))
    after = await spent()
    if after - before != Decimal("0.10"):
        raise ConformanceError(f"重复预留被计了 {after - before}，应为 0.10")
    await guard.release_undispatched(idempotency_key=key)


async def _release_undoes_reservation(guard: BudgetGuard, *, spent: SpentReader, key: str) -> None:
    """预留后没真正发出调用，释放必须把额度还回去。"""

    before = await spent()
    await guard.reserve(idempotency_key=key, estimated_usd=Decimal("0.25"))
    await guard.release_undispatched(idempotency_key=key)
    after = await spent()
    if after != before:
        raise ConformanceError(f"释放后仍占用 {after - before}，应为 0")


async def _settle_replaces_estimate(guard: BudgetGuard, *, spent: SpentReader, key: str) -> None:
    """结算按实际金额覆盖预估，而不是在预估之上再加一笔。"""

    before = await spent()
    await guard.reserve(idempotency_key=key, estimated_usd=Decimal("1.00"))
    await guard.settle(idempotency_key=key, actual_usd=Decimal("0.30"))
    after = await spent()
    if after - before != Decimal("0.30"):
        raise ConformanceError(f"结算后计入 {after - before}，应为实际值 0.30")


async def _release_after_settle_is_noop(
    guard: BudgetGuard, *, spent: SpentReader, key: str
) -> None:
    """已结算的预留再释放不得退款——钱已经花出去了。"""

    await guard.reserve(idempotency_key=key, estimated_usd=Decimal("0.50"))
    await guard.settle(idempotency_key=key, actual_usd=Decimal("0.50"))
    settled = await spent()
    try:
        await guard.release_undispatched(idempotency_key=key)
    except Exception:
        # 显式拒绝也是合规实现：重点是不能把已结算的钱退回去。
        pass
    after = await spent()
    if after != settled:
        raise ConformanceError(f"已结算的预留被退回 {settled - after}")


async def assert_audit_sink_conforms(sink: AuditSink, record: AuditRecord) -> None:
    """`record()` 必须接受完整记录且不得改写调用方的对象。"""

    snapshot = vars(record).copy()
    await sink.record(record)
    if vars(record) != snapshot:
        raise ConformanceError("AuditSink 改写了传入的 AuditRecord")
