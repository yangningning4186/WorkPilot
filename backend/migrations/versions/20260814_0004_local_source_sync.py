"""持久化 local_dir 增量同步游标。

Revision ID: 20260814_0004
Revises: 20260814_0003
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "20260814_0004"
down_revision: str | None = "20260814_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_sources_local_root",
        "sources",
        [sa.text("(config->>'root')")],
        unique=True,
        postgresql_where=sa.text("kind = 'local_dir'"),
    )
    op.create_table(
        "source_sync_entries",
        sa.Column(
            "source_id",
            UUID,
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("source_uri", sa.Text(), primary_key=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("mtime_ns", sa.BigInteger(), nullable=False),
        sa.Column("content_hash", sa.Text()),
        sa.Column("sync_status", sa.Text(), nullable=False),
        sa.Column("sync_error", sa.Text()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "sync_status IN ('synced','failed')", name="ck_source_sync_entries_status"
        ),
        sa.CheckConstraint("size_bytes >= 0 AND mtime_ns >= 0", name="ck_source_sync_entries_stat"),
    )
    op.create_index(
        "idx_source_sync_entries_status", "source_sync_entries", ["source_id", "sync_status"]
    )


def downgrade() -> None:
    op.drop_index("idx_source_sync_entries_status", table_name="source_sync_entries")
    op.drop_table("source_sync_entries")
    op.drop_index("uq_sources_local_root", table_name="sources")
