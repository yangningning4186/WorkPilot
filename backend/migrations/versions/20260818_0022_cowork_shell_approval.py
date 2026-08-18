"""允许 Cowork inbox 持久化一次性 shell 命令审批。

Revision ID: 20260818_0022
Revises: 20260818_0021
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260818_0022"
down_revision: str | None = "20260818_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_cowork_inbox_kind", "cowork_inbox_items", type_="check")
    op.create_check_constraint(
        "ck_cowork_inbox_kind",
        "cowork_inbox_items",
        "kind IN ('ask_user','directory_request','capability_request','shell_approval')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_cowork_inbox_kind", "cowork_inbox_items", type_="check")
    op.create_check_constraint(
        "ck_cowork_inbox_kind",
        "cowork_inbox_items",
        "kind IN ('ask_user','directory_request','capability_request')",
    )
