"""Skill 自动蒸馏候选、独立证据与可靠作业。

Revision ID: 20260819_0026
Revises: 20260819_0025
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260819_0026"
down_revision: str | None = "20260819_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "skill_candidates",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("capability_key", sa.Text(), nullable=False, unique=True),
        sa.Column("suggested_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("skill_md", sa.Text(), nullable=False),
        sa.Column("tools", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="collecting"),
        sa.Column("evidence_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("promoted_name", sa.Text()),
        sa.Column("last_run_id", UUID, sa.ForeignKey("agent_runs.id", ondelete="SET NULL")),
        sa.Column("review_reason", sa.Text()),
        sa.Column("promoted_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('collecting','promoted','needs_review','rejected')",
            name="ck_skill_candidates_status",
        ),
        sa.CheckConstraint("evidence_count >= 0", name="ck_skill_candidates_evidence_count"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_skill_candidates_confidence"),
        sa.CheckConstraint("jsonb_typeof(tools) = 'array'", name="ck_skill_candidates_tools"),
    )
    op.create_index("idx_skill_candidates_status", "skill_candidates", ["status", "updated_at"])
    op.execute(
        """
        CREATE TRIGGER trg_skill_candidates_updated_at
        BEFORE UPDATE ON skill_candidates
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )

    op.create_table(
        "skill_candidate_evidence",
        sa.Column("candidate_id", UUID, sa.ForeignKey("skill_candidates.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("run_id", UUID, sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "skill_distillation_jobs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("run_id", UUID, sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("worker_id", sa.Text()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("candidate_id", UUID, sa.ForeignKey("skill_candidates.id", ondelete="SET NULL")),
        sa.Column("error", sa.Text()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('queued','running','done','failed')",
            name="ck_skill_distillation_jobs_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_skill_distillation_jobs_attempts"),
    )
    op.create_index(
        "idx_skill_distillation_jobs_dispatch",
        "skill_distillation_jobs",
        ["status", "available_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.execute(
        """
        CREATE TRIGGER trg_skill_distillation_jobs_updated_at
        BEFORE UPDATE ON skill_distillation_jobs
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_skill_distillation_jobs_updated_at ON skill_distillation_jobs")
    op.drop_table("skill_distillation_jobs")
    op.drop_table("skill_candidate_evidence")
    op.execute("DROP TRIGGER trg_skill_candidates_updated_at ON skill_candidates")
    op.drop_table("skill_candidates")
