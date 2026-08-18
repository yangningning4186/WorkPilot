"""Cowork 独立网页读取能力。

Revision ID: 20260818_0023
Revises: 20260818_0022
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260818_0023"
down_revision: str | None = "20260818_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.execute(
        "DELETE FROM capability_grants WHERE capability = 'network.read'"
    )
    op.drop_constraint("ck_capability_grants_capability", "capability_grants", type_="check")
    op.drop_constraint("ck_capability_grants_root_required", "capability_grants", type_="check")
    op.create_check_constraint(
        "ck_capability_grants_capability",
        "capability_grants",
        "capability IN ('filesystem.read','filesystem.write','office.word.edit',"
        "'office.excel.edit','shell.execute','external.action')",
    )
    op.create_check_constraint(
        "ck_capability_grants_root_required",
        "capability_grants",
        "capability IN ('shell.execute','external.action') OR session_root_id IS NOT NULL",
    )
