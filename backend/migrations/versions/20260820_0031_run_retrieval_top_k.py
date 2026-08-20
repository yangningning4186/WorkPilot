"""持久化 answer run 的检索深度。

Revision ID: 20260820_0031
Revises: 20260820_0030
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260820_0031"
down_revision: str | None = "20260820_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("retrieval_top_k", sa.Integer(), nullable=False, server_default="5"),
    )
    op.create_check_constraint(
        "ck_agent_runs_retrieval_top_k",
        "agent_runs",
        "retrieval_top_k BETWEEN 1 AND 20",
    )


def downgrade() -> None:
    op.drop_constraint("ck_agent_runs_retrieval_top_k", "agent_runs", type_="check")
    op.drop_column("agent_runs", "retrieval_top_k")
