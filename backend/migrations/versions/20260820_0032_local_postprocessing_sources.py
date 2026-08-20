"""允许 PostgreSQL 后处理作业引用 SQLite Cowork 来源快照。

Revision ID: 20260820_0032
Revises: 20260820_0031
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260820_0032"
down_revision: str | None = "20260820_0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    # SQLite Cowork 的 UUID 是跨 store 的稳定身份，不可能满足 PostgreSQL FK；
    # 作业保存不可变来源快照，run/message UUID 仍用于幂等与审计关联。
    op.drop_constraint("memory_extraction_jobs_run_id_fkey", "memory_extraction_jobs", type_="foreignkey")
    op.drop_constraint(
        "memory_extraction_jobs_source_message_id_fkey",
        "memory_extraction_jobs",
        type_="foreignkey",
    )
    op.drop_constraint("memories_source_message_id_fkey", "memories", type_="foreignkey")
    op.add_column(
        "memory_extraction_jobs",
        sa.Column("source_is_local", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("memory_extraction_jobs", sa.Column("source_conversation_id", UUID))
    op.add_column("memory_extraction_jobs", sa.Column("source_content", sa.Text()))
    op.add_column(
        "memory_extraction_jobs", sa.Column("source_created_at", sa.DateTime(timezone=True))
    )
    op.create_check_constraint(
        "ck_memory_jobs_local_source",
        "memory_extraction_jobs",
        "NOT source_is_local OR (source_conversation_id IS NOT NULL "
        "AND source_content IS NOT NULL AND source_created_at IS NOT NULL)",
    )

    op.drop_constraint("skill_distillation_jobs_run_id_fkey", "skill_distillation_jobs", type_="foreignkey")
    op.drop_constraint("skill_candidates_last_run_id_fkey", "skill_candidates", type_="foreignkey")
    op.drop_constraint("skill_candidate_evidence_run_id_fkey", "skill_candidate_evidence", type_="foreignkey")
    op.add_column(
        "skill_distillation_jobs",
        sa.Column("source_is_local", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("skill_distillation_jobs", sa.Column("source_goal", sa.Text()))
    op.add_column("skill_distillation_jobs", sa.Column("source_final_message", sa.Text()))
    op.add_column(
        "skill_distillation_jobs",
        sa.Column("source_tools", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.create_check_constraint(
        "ck_skill_jobs_local_source",
        "skill_distillation_jobs",
        "NOT source_is_local OR (source_goal IS NOT NULL AND source_final_message IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_skill_jobs_local_source", "skill_distillation_jobs", type_="check")
    op.drop_column("skill_distillation_jobs", "source_tools")
    op.drop_column("skill_distillation_jobs", "source_final_message")
    op.drop_column("skill_distillation_jobs", "source_goal")
    op.drop_column("skill_distillation_jobs", "source_is_local")
    op.create_foreign_key(
        "skill_candidate_evidence_run_id_fkey",
        "skill_candidate_evidence", "agent_runs", ["run_id"], ["id"], ondelete="CASCADE"
    )
    op.create_foreign_key(
        "skill_candidates_last_run_id_fkey",
        "skill_candidates", "agent_runs", ["last_run_id"], ["id"], ondelete="SET NULL"
    )
    op.create_foreign_key(
        "skill_distillation_jobs_run_id_fkey",
        "skill_distillation_jobs", "agent_runs", ["run_id"], ["id"], ondelete="CASCADE"
    )

    op.drop_constraint("ck_memory_jobs_local_source", "memory_extraction_jobs", type_="check")
    op.drop_column("memory_extraction_jobs", "source_created_at")
    op.drop_column("memory_extraction_jobs", "source_content")
    op.drop_column("memory_extraction_jobs", "source_conversation_id")
    op.drop_column("memory_extraction_jobs", "source_is_local")
    op.create_foreign_key(
        "memories_source_message_id_fkey",
        "memories", "messages", ["source_message_id"], ["id"], ondelete="SET NULL"
    )
    op.create_foreign_key(
        "memory_extraction_jobs_source_message_id_fkey",
        "memory_extraction_jobs", "messages", ["source_message_id"], ["id"], ondelete="CASCADE"
    )
    op.create_foreign_key(
        "memory_extraction_jobs_run_id_fkey",
        "memory_extraction_jobs", "agent_runs", ["run_id"], ["id"], ondelete="CASCADE"
    )
