"""Conversation archiving for Cowork clients.

Revision ID: 20260820_0028
Revises: 20260819_0027
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260820_0028"
down_revision: str | None = "20260819_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_conversations_archived",
        "conversations",
        ["scope", "archived_at", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_conversations_archived", table_name="conversations")
    op.drop_column("conversations", "archived_at")
