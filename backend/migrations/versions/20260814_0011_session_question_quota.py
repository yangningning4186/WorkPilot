"""记录 demo session 已消费的提问配额。

Revision ID: 20260814_0011
Revises: 20260814_0010
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260814_0011"
down_revision: str | None = "20260814_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "demo_sessions",
        sa.Column("question_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_demo_sessions_question_count",
        "demo_sessions",
        "question_count >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_demo_sessions_question_count", "demo_sessions", type_="check"
    )
    op.drop_column("demo_sessions", "question_count")
