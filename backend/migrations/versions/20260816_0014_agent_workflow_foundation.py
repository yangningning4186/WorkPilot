"""增加固定综述 Agent 的执行状态与副作用幂等表。

Revision ID: 20260816_0014
Revises: 20260815_0013
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260816_0014"
down_revision: str | None = "20260815_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column(
            "workflow_type",
            sa.Text(),
            nullable=False,
            server_default="answer",
        ),
    )
    op.create_check_constraint(
        "ck_agent_runs_workflow_type",
        "agent_runs",
        "workflow_type IN ('answer', 'literature_review')",
    )

    op.create_table(
        "agent_plan_steps",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "run_id",
            UUID,
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step_idx", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("tool", sa.Text()),
        sa.Column(
            "depends_on",
            postgresql.ARRAY(sa.Integer()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("step_idx >= 0", name="ck_agent_plan_steps_index"),
        sa.CheckConstraint("length(description) > 0", name="ck_agent_plan_steps_description"),
        sa.CheckConstraint(
            "status IN ('pending','running','done','failed','skipped')",
            name="ck_agent_plan_steps_status",
        ),
        sa.UniqueConstraint("run_id", "step_idx", name="uq_agent_plan_steps_run_index"),
    )
    op.create_index("idx_agent_plan_steps_run_status", "agent_plan_steps", ["run_id", "status"])

    op.create_table(
        "tool_invocations",
        sa.Column("idempotency_key", sa.Text(), primary_key=True),
        sa.Column(
            "run_id",
            UUID,
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("args_hash", sa.Text(), nullable=False),
        sa.Column("result", JSONB),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("lease_owner", sa.Text()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("effect_ref", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("length(tool_name) > 0", name="ck_tool_invocations_tool_name"),
        sa.CheckConstraint(
            "status IN ('in_flight','succeeded','failed')",
            name="ck_tool_invocations_status",
        ),
        sa.CheckConstraint("retry_count >= 0", name="ck_tool_invocations_retry_count"),
        sa.CheckConstraint(
            "status <> 'in_flight' OR (lease_owner IS NOT NULL AND lease_until IS NOT NULL)",
            name="ck_tool_invocations_active_lease",
        ),
    )
    op.create_index(
        "idx_tool_invocations_recovery",
        "tool_invocations",
        ["lease_until"],
        postgresql_where=sa.text("status = 'in_flight'"),
    )

    op.create_table(
        "agent_attempts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "run_id",
            UUID,
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "plan_step_id",
            UUID,
            sa.ForeignKey("agent_plan_steps.id", ondelete="CASCADE"),
        ),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("node", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.Text()),
        sa.Column("tool_args", JSONB),
        sa.Column("tool_result", JSONB),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "idempotency_key",
            sa.Text(),
            sa.ForeignKey("tool_invocations.idempotency_key"),
        ),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("tokens", sa.Integer()),
        sa.Column("error_model", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("attempt_no >= 1", name="ck_agent_attempts_number"),
        sa.CheckConstraint("length(node) > 0", name="ck_agent_attempts_node"),
        sa.CheckConstraint(
            "status IN ('ok','retry','failed','skipped','fallback')",
            name="ck_agent_attempts_status",
        ),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0", name="ck_agent_attempts_latency"
        ),
        sa.CheckConstraint("tokens IS NULL OR tokens >= 0", name="ck_agent_attempts_tokens"),
    )
    # planner 阶段 plan_step_id 可以为 NULL；NULLS NOT DISTINCT 仍能防止重复 attempt。
    op.execute(
        """
        CREATE UNIQUE INDEX uq_agent_attempts_attempt
        ON agent_attempts (run_id, plan_step_id, attempt_no, node) NULLS NOT DISTINCT
        """
    )
    op.create_index("idx_agent_attempts_run_created", "agent_attempts", ["run_id", "created_at"])

    op.create_table(
        "agent_checkpoints",
        sa.Column(
            "run_id",
            UUID,
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("checkpoint_id", sa.Text(), primary_key=True),
        sa.Column("parent_id", sa.Text()),
        sa.Column("state", JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("length(checkpoint_id) > 0", name="ck_agent_checkpoints_id"),
    )
    op.create_index(
        "idx_agent_checkpoints_latest", "agent_checkpoints", ["run_id", "created_at"]
    )

    for table_name in ("agent_plan_steps", "tool_invocations"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_updated_at
            BEFORE UPDATE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
            """
        )


def downgrade() -> None:
    for table_name in ("tool_invocations", "agent_plan_steps"):
        op.execute(f"DROP TRIGGER trg_{table_name}_updated_at ON {table_name}")
    op.drop_table("agent_checkpoints")
    op.drop_table("agent_attempts")
    op.drop_table("tool_invocations")
    op.drop_table("agent_plan_steps")
    op.drop_constraint("ck_agent_runs_workflow_type", "agent_runs", type_="check")
    op.drop_column("agent_runs", "workflow_type")
