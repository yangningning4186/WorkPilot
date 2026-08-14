"""增加取消请求标记与 watchdog 扫描索引。

Revision ID: 20260814_0007
Revises: 20260814_0006
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260814_0007"
down_revision: str | None = "20260814_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 app/services/runs.py 的 TERMINAL_RUN_STATUSES 保持一致。
_ACTIVE_RUN_PREDICATE = "status NOT IN ('budget_exceeded', 'cancelled', 'done', 'failed')"


def upgrade() -> None:
    # 取消是"请求"而不是"立即终态": run 正在执行时直接改 status 会造成
    # 终态之后仍在写事件的不一致, 必须让持有租约的 worker 自己收尾。
    op.add_column(
        "agent_runs",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True)),
    )
    # watchdog 每次只扫租约过期的活跃 run, 部分索引避免全表扫。
    op.create_index(
        "idx_agent_runs_lease",
        "agent_runs",
        ["lease_until"],
        postgresql_where=sa.text(f"{_ACTIVE_RUN_PREDICATE} AND lease_until IS NOT NULL"),
    )
    # 列表页按对话取最近的 run。
    op.create_index("idx_agent_runs_conversation", "agent_runs", ["conversation_id", "created_at"])


def downgrade() -> None:
    op.drop_index("idx_agent_runs_conversation", table_name="agent_runs")
    op.drop_index("idx_agent_runs_lease", table_name="agent_runs")
    op.drop_column("agent_runs", "cancel_requested_at")
