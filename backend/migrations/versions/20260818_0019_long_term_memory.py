"""owner 长期记忆与幂等抽取作业。

Revision ID: 20260818_0019
Revises: 20260818_0018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "20260818_0019"
down_revision: str | None = "20260818_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memories",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("fact", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1024)),
        sa.Column("embedding_model", sa.Text()),
        sa.Column("embedding_provider", sa.Text()),
        sa.Column("embedding_revision", sa.Text()),
        sa.Column(
            "valid_from", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("invalid_at", sa.DateTime(timezone=True)),
        sa.Column("superseded_by", UUID),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column(
            "source_message_id",
            UUID,
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "confidence", sa.Float(), nullable=False, server_default=sa.text("1.0")
        ),
        sa.Column("access_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "category IN ('preference','profile','interest','fact')",
            name="ck_memories_category",
        ),
        sa.CheckConstraint(
            "source_type IN ('conversation','manual')",
            name="ck_memories_source_type",
        ),
        sa.CheckConstraint(
            "length(btrim(fact)) > 0 AND length(fact) <= 2000",
            name="ck_memories_fact",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_memories_confidence",
        ),
        sa.CheckConstraint(
            "invalid_at IS NULL OR invalid_at > valid_from",
            name="ck_memories_validity",
        ),
        sa.CheckConstraint(
            "superseded_by IS NULL OR superseded_by <> id",
            name="ck_memories_not_self_superseded",
        ),
        sa.CheckConstraint(
            "(embedding IS NULL AND embedding_model IS NULL "
            "AND embedding_provider IS NULL AND embedding_revision IS NULL) OR "
            "(embedding IS NOT NULL AND embedding_model IS NOT NULL "
            "AND embedding_provider IS NOT NULL AND embedding_revision IS NOT NULL)",
            name="ck_memories_embedding_identity",
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by"],
            ["memories.id"],
            name="fk_memories_superseded_by",
            ondelete="SET NULL",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    op.create_index(
        "idx_memories_active_category",
        "memories",
        ["category", "valid_from"],
        postgresql_where=sa.text("invalid_at IS NULL"),
    )
    op.create_index(
        "idx_memories_active_vector",
        "memories",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_where=sa.text("invalid_at IS NULL AND embedding IS NOT NULL"),
    )
    op.create_index("idx_memories_source_message", "memories", ["source_message_id"])
    op.execute(
        """
        CREATE TRIGGER trg_memories_updated_at
        BEFORE UPDATE ON memories
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )

    op.create_table(
        "memory_extraction_jobs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "run_id",
            UUID,
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "source_message_id",
            UUID,
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("extractor_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("worker_id", sa.Text()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("operations", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("error", sa.Text()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','done','failed')",
            name="ck_memory_jobs_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_memory_jobs_attempts"),
        sa.CheckConstraint(
            "jsonb_typeof(operations) = 'array'",
            name="ck_memory_jobs_operations",
        ),
    )
    op.create_index(
        "idx_memory_jobs_dispatch",
        "memory_extraction_jobs",
        ["status", "available_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.execute(
        """
        CREATE TRIGGER trg_memory_extraction_jobs_updated_at
        BEFORE UPDATE ON memory_extraction_jobs
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER trg_memory_extraction_jobs_updated_at ON memory_extraction_jobs"
    )
    op.drop_table("memory_extraction_jobs")
    op.execute("DROP TRIGGER trg_memories_updated_at ON memories")
    op.drop_table("memories")
