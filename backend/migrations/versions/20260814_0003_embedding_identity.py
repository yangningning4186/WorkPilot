"""记录 embedding 向量空间身份并禁止混检。

Revision ID: 20260814_0003
Revises: 20260814_0002
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260814_0003"
down_revision: str | None = "20260814_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for column_name in ("embedding_model", "embedding_provider", "embedding_revision"):
        op.add_column(
            "document_versions",
            sa.Column(column_name, sa.Text(), nullable=False, server_default="legacy-unknown"),
        )
        op.alter_column("document_versions", column_name, server_default=None)
        op.add_column("chunks", sa.Column(column_name, sa.Text()))

    op.execute(
        """
        UPDATE chunks
        SET embedding_model = 'legacy-unknown',
            embedding_provider = 'legacy-unknown',
            embedding_revision = 'legacy-unknown'
        WHERE embedding IS NOT NULL
        """
    )
    op.create_check_constraint(
        "ck_chunks_embedding_identity",
        "chunks",
        "embedding IS NULL OR (embedding_model IS NOT NULL "
        "AND embedding_provider IS NOT NULL AND embedding_revision IS NOT NULL)",
    )
    op.create_index(
        "idx_chunk_embedding_identity",
        "chunks",
        ["embedding_model", "embedding_provider", "embedding_revision"],
        postgresql_where=sa.text("embedding IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_chunk_embedding_identity", table_name="chunks")
    op.drop_constraint("ck_chunks_embedding_identity", "chunks", type_="check")
    for column_name in reversed(("embedding_model", "embedding_provider", "embedding_revision")):
        op.drop_column("chunks", column_name)
        op.drop_column("document_versions", column_name)
