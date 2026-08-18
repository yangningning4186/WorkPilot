"""Provider、连接器/OAuth 与会话级运行偏好。

Revision ID: 20260819_0025
Revises: 20260819_0024
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260819_0025"
down_revision: str | None = "20260819_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.drop_constraint("ck_cowork_inbox_kind", "cowork_inbox_items", type_="check")
    op.create_check_constraint(
        "ck_cowork_inbox_kind",
        "cowork_inbox_items",
        "kind IN ('ask_user','directory_request','capability_request','shell_approval',"
        "'external_approval')",
    )
    op.create_table(
        "provider_profiles",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("default_model", sa.Text(), nullable=False),
        sa.Column("api_key_ciphertext", sa.Text()),
        sa.Column("context_window_tokens", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "provider IN ('openai','anthropic','gemini','deepseek','qwen','ollama',"
            "'openai_compatible')",
            name="ck_provider_profiles_provider",
        ),
        sa.CheckConstraint("length(btrim(name)) BETWEEN 1 AND 80", name="ck_provider_name"),
        sa.CheckConstraint(
            "length(btrim(base_url)) BETWEEN 1 AND 2048", name="ck_provider_base_url"
        ),
        sa.CheckConstraint(
            "length(btrim(default_model)) BETWEEN 1 AND 200", name="ck_provider_model"
        ),
        sa.CheckConstraint(
            "context_window_tokens BETWEEN 1024 AND 2000000",
            name="ck_provider_context_window",
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_provider_profiles_name_ci "
        "ON provider_profiles (lower(name))"
    )

    op.add_column(
        "conversations",
        sa.Column("provider_profile_id", UUID, nullable=True),
    )
    op.add_column("conversations", sa.Column("model_override", sa.Text(), nullable=True))
    op.add_column(
        "conversations",
        sa.Column("unattended", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_foreign_key(
        "fk_conversations_provider_profile",
        "conversations",
        "provider_profiles",
        ["provider_profile_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_conversations_model_override",
        "conversations",
        "model_override IS NULL OR length(btrim(model_override)) BETWEEN 1 AND 200",
    )

    op.create_table(
        "connector_accounts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("auth_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="configured"),
        sa.Column("config", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("secret_ciphertext", sa.Text()),
        sa.Column("scopes", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("external_account_id", sa.Text()),
        sa.Column("external_account_name", sa.Text()),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "kind IN ('github','feishu','wecom','wechat_official','tencent_docs')",
            name="ck_connector_accounts_kind",
        ),
        sa.CheckConstraint(
            "auth_type IN ('oauth2','token','app_credentials')",
            name="ck_connector_accounts_auth_type",
        ),
        sa.CheckConstraint(
            "status IN ('configured','authorizing','connected','expired','error')",
            name="ck_connector_accounts_status",
        ),
        sa.CheckConstraint(
            "length(btrim(name)) BETWEEN 1 AND 100", name="ck_connector_accounts_name"
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_connector_accounts_kind_name_ci "
        "ON connector_accounts (kind, lower(name))"
    )

    op.create_table(
        "oauth_states",
        sa.Column("state", sa.Text(), primary_key=True),
        sa.Column(
            "connector_account_id",
            UUID,
            sa.ForeignKey("connector_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("code_verifier_ciphertext", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(state) BETWEEN 32 AND 256", name="ck_oauth_states_state"),
    )
    op.create_index("idx_oauth_states_expiry", "oauth_states", ["expires_at"])

    for table_name in ("provider_profiles", "connector_accounts"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_updated_at
            BEFORE UPDATE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
            """
        )


def downgrade() -> None:
    for table_name in ("connector_accounts", "provider_profiles"):
        op.execute(f"DROP TRIGGER trg_{table_name}_updated_at ON {table_name}")
    op.drop_table("oauth_states")
    op.drop_table("connector_accounts")
    op.drop_constraint("ck_conversations_model_override", "conversations", type_="check")
    op.drop_constraint("fk_conversations_provider_profile", "conversations", type_="foreignkey")
    op.drop_column("conversations", "unattended")
    op.drop_column("conversations", "model_override")
    op.drop_column("conversations", "provider_profile_id")
    op.drop_table("provider_profiles")
    op.drop_constraint("ck_cowork_inbox_kind", "cowork_inbox_items", type_="check")
    op.create_check_constraint(
        "ck_cowork_inbox_kind",
        "cowork_inbox_items",
        "kind IN ('ask_user','directory_request','capability_request','shell_approval')",
    )
