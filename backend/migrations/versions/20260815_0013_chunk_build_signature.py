"""记录离线分块产物的可重复构建身份。

Revision ID: 20260815_0013
Revises: 20260814_0012
Create Date: 2026-08-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260815_0013"
down_revision: str | None = "20260814_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # heading 是历史在线入库产物, 允许为空; 三套离线策略始终写入完整签名。
    op.add_column("chunks", sa.Column("build_signature", sa.Text()))


def downgrade() -> None:
    op.drop_column("chunks", "build_signature")
