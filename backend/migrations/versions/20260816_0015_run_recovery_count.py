"""记录 run 被 watchdog 自动恢复过几次。

Revision ID: 20260816_0015
Revises: 20260816_0014
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260816_0015"
down_revision: str | None = "20260816_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 没有这个计数, 一个稳定把 worker 拖垮的 run 会被 watchdog 无限重新入队。
    op.add_column(
        "agent_runs",
        sa.Column("recovery_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_agent_runs_recovery_count", "agent_runs", "recovery_count >= 0"
    )


def downgrade() -> None:
    op.drop_constraint("ck_agent_runs_recovery_count", "agent_runs", type_="check")
    op.drop_column("agent_runs", "recovery_count")
