"""计划模式的批准请求。

`plan_approval` 走的是和 ask_user / shell_approval 同一套 inbox + resume 机制，
只需要放开 kind 的 CHECK；批准之后翻转的是 checkpoint 里的 `mode`，不落表。

Revision ID: 20260820_0036
Revises: 20260820_0035
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260820_0036"
down_revision: str | None = "20260820_0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = "'ask_user','directory_request','capability_request','shell_approval','external_approval'"


def upgrade() -> None:
    op.drop_constraint("ck_cowork_inbox_kind", "cowork_inbox_items", type_="check")
    op.create_check_constraint(
        "ck_cowork_inbox_kind",
        "cowork_inbox_items",
        f"kind IN ({_KINDS},'plan_approval')",
    )


def downgrade() -> None:
    # 旧 CHECK 容不下这些行，回滚只能删掉它们；等待中的计划请求会随之失去
    # 恢复入口，对应的 run 需要重跑。
    op.execute("DELETE FROM cowork_inbox_items WHERE kind = 'plan_approval'")
    op.drop_constraint("ck_cowork_inbox_kind", "cowork_inbox_items", type_="check")
    op.create_check_constraint(
        "ck_cowork_inbox_kind",
        "cowork_inbox_items",
        f"kind IN ({_KINDS})",
    )
