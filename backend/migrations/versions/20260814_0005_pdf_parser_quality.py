"""记录 PDF 解析选择与增量入库身份。

Revision ID: 20260814_0005
Revises: 20260814_0004
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "20260814_0005"
down_revision: str | None = "20260814_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "document_versions",
        sa.Column(
            "parse_meta",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column("source_sync_entries", sa.Column("ingest_signature", sa.Text()))


def downgrade() -> None:
    op.drop_column("source_sync_entries", "ingest_signature")
    op.drop_column("document_versions", "parse_meta")
