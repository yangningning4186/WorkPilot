"""Cowork 会话目录、能力授权与交付物。

Revision ID: 20260818_0020
Revises: 20260818_0019
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260818_0020"
down_revision: str | None = "20260818_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.drop_constraint("ck_agent_runs_workflow_type", "agent_runs", type_="check")
    op.create_check_constraint(
        "ck_agent_runs_workflow_type",
        "agent_runs",
        "workflow_type IN ('answer', 'literature_review', 'cowork')",
    )

    op.create_table(
        "session_roots",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "conversation_id",
            UUID,
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("requested_path", sa.Text(), nullable=False),
        sa.Column("canonical_path", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("access_mode", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "access_mode IN ('read_only','read_write')",
            name="ck_session_roots_access_mode",
        ),
        sa.CheckConstraint(
            "length(btrim(canonical_path)) > 0",
            name="ck_session_roots_canonical_path",
        ),
        sa.UniqueConstraint(
            "conversation_id", "canonical_path", name="uq_session_roots_conversation_path"
        ),
        sa.UniqueConstraint("id", "conversation_id", name="uq_session_roots_id_conversation"),
    )
    op.create_index("idx_session_roots_conversation", "session_roots", ["conversation_id"])

    op.create_table(
        "capability_grants",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("conversation_id", UUID, nullable=False),
        sa.Column("session_root_id", UUID),
        sa.Column("capability", sa.Text(), nullable=False),
        sa.Column("grant_source", sa.Text(), nullable=False, server_default="user"),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["session_root_id", "conversation_id"],
            ["session_roots.id", "session_roots.conversation_id"],
            name="fk_capability_grants_root_conversation",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "capability IN ('filesystem.read','filesystem.write','office.word.edit',"
            "'office.excel.edit','shell.execute','external.action')",
            name="ck_capability_grants_capability",
        ),
        sa.CheckConstraint(
            "grant_source IN ('user','policy')", name="ck_capability_grants_source"
        ),
        sa.CheckConstraint(
            "capability IN ('shell.execute','external.action') OR session_root_id IS NOT NULL",
            name="ck_capability_grants_root_required",
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="ck_capability_grants_revoked_at",
        ),
    )
    op.create_index(
        "idx_capability_grants_active",
        "capability_grants",
        ["conversation_id", "capability"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_capability_grants_active
        ON capability_grants (conversation_id, session_root_id, capability) NULLS NOT DISTINCT
        WHERE revoked_at IS NULL
        """
    )

    op.create_table(
        "artifacts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "conversation_id",
            UUID,
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id", UUID, sa.ForeignKey("agent_runs.id", ondelete="SET NULL")
        ),
        sa.Column("session_root_id", UUID),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text()),
        sa.Column("meta", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["session_root_id", "conversation_id"],
            ["session_roots.id", "session_roots.conversation_id"],
            name="fk_artifacts_root_conversation",
        ),
        sa.CheckConstraint(
            "kind IN ('file','report','diff','table')", name="ck_artifacts_kind"
        ),
        sa.CheckConstraint("length(btrim(title)) > 0", name="ck_artifacts_title"),
        sa.CheckConstraint("length(btrim(uri)) > 0", name="ck_artifacts_uri"),
    )
    op.create_index(
        "idx_artifacts_conversation_created",
        "artifacts",
        ["conversation_id", "created_at"],
    )
    op.create_index("idx_artifacts_run", "artifacts", ["run_id"])

    for table_name in ("session_roots", "capability_grants", "artifacts"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_updated_at
            BEFORE UPDATE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
            """
        )


def downgrade() -> None:
    for table_name in ("artifacts", "capability_grants", "session_roots"):
        op.execute(f"DROP TRIGGER trg_{table_name}_updated_at ON {table_name}")
    op.drop_table("artifacts")
    op.drop_table("capability_grants")
    op.drop_table("session_roots")
    op.drop_constraint("ck_agent_runs_workflow_type", "agent_runs", type_="check")
    op.create_check_constraint(
        "ck_agent_runs_workflow_type",
        "agent_runs",
        "workflow_type IN ('answer', 'literature_review')",
    )
