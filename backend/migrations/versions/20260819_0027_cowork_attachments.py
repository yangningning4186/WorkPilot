"""Cowork 输入附件与消息绑定。

Revision ID: 20260819_0027
Revises: 20260819_0026
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260819_0027"
down_revision: str | None = "20260819_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "cowork_attachments",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "conversation_id",
            UUID,
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "message_id",
            UUID,
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "run_id",
            UUID,
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False, unique=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("extracted_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("kind IN ('image','pdf','text')", name="ck_cowork_attachments_kind"),
        sa.CheckConstraint("size_bytes > 0", name="ck_cowork_attachments_size"),
        sa.CheckConstraint(
            "(message_id IS NULL AND run_id IS NULL) OR (message_id IS NOT NULL AND run_id IS NOT NULL)",
            name="ck_cowork_attachments_binding",
        ),
    )
    op.create_index(
        "idx_cowork_attachments_conversation",
        "cowork_attachments",
        ["conversation_id", "created_at"],
    )
    op.create_index(
        "idx_cowork_attachments_run",
        "cowork_attachments",
        ["run_id", "created_at"],
        postgresql_where=sa.text("run_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("cowork_attachments")
