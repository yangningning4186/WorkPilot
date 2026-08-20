"""新增独立的个人资料库读取授权。

Revision ID: 20260820_0033
Revises: 20260820_0032
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260820_0033"
down_revision: str | None = "20260820_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_capability_grants_capability", "capability_grants", type_="check")
    op.drop_constraint("ck_capability_grants_root_required", "capability_grants", type_="check")
    op.create_check_constraint(
        "ck_capability_grants_capability",
        "capability_grants",
        "capability IN ('knowledge.read','filesystem.read','filesystem.write',"
        "'office.word.edit','office.excel.edit','network.read','shell.execute','external.action')",
    )
    op.create_check_constraint(
        "ck_capability_grants_root_required",
        "capability_grants",
        "capability IN ('knowledge.read','network.read','shell.execute','external.action') "
        "OR session_root_id IS NOT NULL",
    )


def downgrade() -> None:
    op.execute("DELETE FROM capability_grants WHERE capability = 'knowledge.read'")
    op.drop_constraint("ck_capability_grants_capability", "capability_grants", type_="check")
    op.drop_constraint("ck_capability_grants_root_required", "capability_grants", type_="check")
    op.create_check_constraint(
        "ck_capability_grants_capability",
        "capability_grants",
        "capability IN ('filesystem.read','filesystem.write','office.word.edit',"
        "'office.excel.edit','network.read','shell.execute','external.action')",
    )
    op.create_check_constraint(
        "ck_capability_grants_root_required",
        "capability_grants",
        "capability IN ('network.read','shell.execute','external.action') "
        "OR session_root_id IS NOT NULL",
    )
