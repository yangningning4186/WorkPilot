"""记录一次 run 用的是资料库回答还是通用知识回答。

Revision ID: 20260814_0012
Revises: 20260814_0011
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260814_0012"
down_revision: str | None = "20260814_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 模式必须落库而不是只放在队列参数里: 它决定了这条回答是否可溯源,
    # 是事后审计和评测分层(grounded / general 分开统计)的依据, 而队列消息是易失的。
    op.add_column(
        "agent_runs",
        sa.Column(
            "answer_mode",
            sa.Text(),
            nullable=False,
            server_default="grounded",
        ),
    )
    op.create_check_constraint(
        "ck_agent_runs_answer_mode",
        "agent_runs",
        "answer_mode IN ('grounded', 'general')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_agent_runs_answer_mode", "agent_runs", type_="check")
    op.drop_column("agent_runs", "answer_mode")
