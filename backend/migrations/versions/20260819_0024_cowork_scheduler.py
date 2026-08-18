"""Cowork scheduler 与 unattended inbox。

Revision ID: 20260819_0024
Revises: 20260818_0023
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260819_0024"
down_revision: str | None = "20260818_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "cowork_schedules",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "conversation_id",
            UUID,
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("schedule_kind", sa.Text(), nullable=False),
        sa.Column("cron_expression", sa.Text()),
        sa.Column("run_at", sa.DateTime(timezone=True)),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        sa.Column("last_run_at", sa.DateTime(timezone=True)),
        sa.Column("last_run_id", UUID),
        sa.Column("run_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["last_run_id"], ["agent_runs.id"], ondelete="SET NULL"
        ),
        sa.CheckConstraint(
            "schedule_kind IN ('once','cron')", name="ck_cowork_schedules_kind"
        ),
        sa.CheckConstraint(
            "(schedule_kind = 'once' AND run_at IS NOT NULL AND cron_expression IS NULL) OR "
            "(schedule_kind = 'cron' AND run_at IS NULL AND cron_expression IS NOT NULL)",
            name="ck_cowork_schedules_shape",
        ),
        sa.CheckConstraint(
            "length(btrim(title)) BETWEEN 1 AND 200", name="ck_cowork_schedules_title"
        ),
        sa.CheckConstraint(
            "length(btrim(goal)) BETWEEN 1 AND 4000", name="ck_cowork_schedules_goal"
        ),
        sa.CheckConstraint("run_count >= 0", name="ck_cowork_schedules_run_count"),
        sa.CheckConstraint("skipped_count >= 0", name="ck_cowork_schedules_skipped_count"),
    )
    op.create_index(
        "idx_cowork_schedules_due",
        "cowork_schedules",
        ["next_run_at", "id"],
        postgresql_where=sa.text("enabled = true AND next_run_at IS NOT NULL"),
    )
    op.create_index(
        "idx_cowork_schedules_conversation",
        "cowork_schedules",
        ["conversation_id", "created_at"],
    )

    op.add_column(
        "agent_runs", sa.Column("schedule_id", UUID, nullable=True)
    )
    op.add_column(
        "agent_runs",
        sa.Column("unattended", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "agent_runs",
        sa.Column("run_trigger", sa.Text(), nullable=False, server_default="manual"),
    )
    op.create_foreign_key(
        "fk_agent_runs_schedule",
        "agent_runs",
        "cowork_schedules",
        ["schedule_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_agent_runs_trigger",
        "agent_runs",
        "run_trigger IN ('manual','schedule','catchup')",
    )
    op.create_index("idx_agent_runs_schedule", "agent_runs", ["schedule_id", "created_at"])

    op.add_column(
        "cowork_inbox_items",
        sa.Column("unattended", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "idx_cowork_inbox_unattended_pending",
        "cowork_inbox_items",
        ["created_at", "id"],
        postgresql_where=sa.text("unattended = true AND status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("idx_cowork_inbox_unattended_pending", table_name="cowork_inbox_items")
    op.drop_column("cowork_inbox_items", "unattended")
    op.drop_index("idx_agent_runs_schedule", table_name="agent_runs")
    op.drop_constraint("ck_agent_runs_trigger", "agent_runs", type_="check")
    op.drop_constraint("fk_agent_runs_schedule", "agent_runs", type_="foreignkey")
    op.drop_column("agent_runs", "run_trigger")
    op.drop_column("agent_runs", "unattended")
    op.drop_column("agent_runs", "schedule_id")
    op.drop_table("cowork_schedules")
