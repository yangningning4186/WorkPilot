"""Cowork steering 队列与运行中交互 inbox。

Revision ID: 20260818_0021
Revises: 20260818_0020
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260818_0021"
down_revision: str | None = "20260818_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "cowork_steering_messages",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "run_id",
            UUID,
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            UUID,
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending','consumed','cancelled')",
            name="ck_cowork_steering_status",
        ),
        sa.CheckConstraint(
            "length(btrim(content)) BETWEEN 1 AND 4000",
            name="ck_cowork_steering_content",
        ),
    )
    op.create_index(
        "idx_cowork_steering_pending",
        "cowork_steering_messages",
        ["run_id", "created_at", "id"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "cowork_inbox_items",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "run_id",
            UUID,
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            UUID,
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("resume_token", UUID, nullable=False, unique=True),
        sa.Column("tool_call_id", sa.Text(), nullable=False),
        sa.Column(
            "plan_step_id",
            UUID,
            sa.ForeignKey("agent_plan_steps.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request", JSONB, nullable=False),
        sa.Column("response", JSONB),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("responded_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "kind IN ('ask_user','directory_request','capability_request')",
            name="ck_cowork_inbox_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending','answered','approved','rejected','cancelled')",
            name="ck_cowork_inbox_status",
        ),
        sa.CheckConstraint(
            "length(btrim(tool_call_id)) > 0",
            name="ck_cowork_inbox_tool_call_id",
        ),
        sa.UniqueConstraint("run_id", "tool_call_id", name="uq_cowork_inbox_tool_call"),
    )
    op.create_index("idx_cowork_inbox_run_created", "cowork_inbox_items", ["run_id", "created_at"])
    op.create_index(
        "uq_cowork_inbox_pending_run",
        "cowork_inbox_items",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_table("cowork_inbox_items")
    op.drop_table("cowork_steering_messages")
