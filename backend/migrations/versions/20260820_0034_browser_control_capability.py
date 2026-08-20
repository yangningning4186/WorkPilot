"""新增会话级浏览器交互授权，替代逐动作审批。

浏览器的导航、点击、输入、选择原本每次都要走 external_approval，一次浏览任务
需要十几轮人工往返，unattended 计划则永远停在 inbox。改成一次会话级授权后，
逐动作审批只保留给会把本地文件外发的 browser_upload。

Revision ID: 20260820_0034
Revises: 20260820_0033
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260820_0034"
down_revision: str | None = "20260820_0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GLOBAL_BEFORE = "'knowledge.read','network.read','shell.execute','external.action'"
_GLOBAL_AFTER = (
    "'knowledge.read','network.read','browser.control','shell.execute','external.action'"
)
_ALL_BEFORE = (
    "'knowledge.read','filesystem.read','filesystem.write','office.word.edit',"
    "'office.excel.edit','network.read','shell.execute','external.action'"
)
_ALL_AFTER = (
    "'knowledge.read','filesystem.read','filesystem.write','office.word.edit',"
    "'office.excel.edit','network.read','browser.control','shell.execute','external.action'"
)


def _rewrite(*, allowed: str, global_only: str) -> None:
    op.drop_constraint("ck_capability_grants_capability", "capability_grants", type_="check")
    op.drop_constraint("ck_capability_grants_root_required", "capability_grants", type_="check")
    op.create_check_constraint(
        "ck_capability_grants_capability",
        "capability_grants",
        f"capability IN ({allowed})",
    )
    op.create_check_constraint(
        "ck_capability_grants_root_required",
        "capability_grants",
        f"capability IN ({global_only}) OR session_root_id IS NOT NULL",
    )


def upgrade() -> None:
    _rewrite(allowed=_ALL_AFTER, global_only=_GLOBAL_AFTER)


def downgrade() -> None:
    op.execute("DELETE FROM capability_grants WHERE capability = 'browser.control'")
    _rewrite(allowed=_ALL_BEFORE, global_only=_GLOBAL_BEFORE)
