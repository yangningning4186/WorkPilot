"""workpilot-telemetry —— 三层之外旁挂的横切包：「只懂度量」。

对齐 pi 的 `pi-telemetry`。这里定义**被测量的东西长什么样**（`AuditRecord`）、
**成本口径怎么算**（`BatchCost` 的摊销公式）、以及**费用闸门的契约**
（`BudgetGuard` / `CostReservation`）。

它不知道这些数字要写进哪张表、上报到哪个后端——那些是 `app/telemetry/` 里的适配器。
依赖表为空是刻意的：成本口径是本项目最需要"说得清、算得准、换个存储也不变"的东西，
一旦这个包里出现 SQLAlchemy，口径就会开始跟着表结构走。
"""

from workpilot_telemetry.budget import (
    BudgetExceededError,
    BudgetGuard,
    CostReservation,
    IdempotencyConflictError,
    InvalidReservationTransitionError,
)
from workpilot_telemetry.cost import BatchCost
from workpilot_telemetry.records import AuditRecord, AuditSink

__all__ = [
    "AuditRecord",
    "AuditSink",
    "BatchCost",
    "BudgetExceededError",
    "BudgetGuard",
    "CostReservation",
    "IdempotencyConflictError",
    "InvalidReservationTransitionError",
]
