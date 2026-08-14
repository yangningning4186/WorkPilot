"""为公网 demo 增加匿名 session 与 conversation 所有权。

Revision ID: 20260814_0010
Revises: 20260814_0009
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "20260814_0010"
down_revision: str | None = "20260814_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "demo_sessions",
        sa.Column("id", UUID, primary_key=True),
        # 浏览器只持有高熵原始 token; 数据库泄漏时不能直接拿 hash 冒充 cookie。
        sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("expires_at > created_at", name="ck_demo_sessions_expiry"),
    )
    op.create_index("idx_demo_sessions_expiry", "demo_sessions", ["expires_at"])

    # 早期 schema 已允许写 demo_session_id, 但没有 token 可恢复。保留这些对话,
    # 同时把对应 legacy session 标为 revoked, 避免迁移后被新浏览器碰巧接管。
    op.execute(
        """
        INSERT INTO demo_sessions (id, token_hash, expires_at, revoked_at)
        SELECT DISTINCT demo_session_id,
               'legacy:' || demo_session_id::text,
               now() + interval '1 second',
               now()
        FROM conversations
        WHERE scope = 'demo' AND demo_session_id IS NOT NULL
        """
    )
    op.create_foreign_key(
        "fk_conversations_demo_session",
        "conversations",
        "demo_sessions",
        ["demo_session_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("fk_conversations_demo_session", "conversations", type_="foreignkey")
    op.drop_index("idx_demo_sessions_expiry", table_name="demo_sessions")
    op.drop_table("demo_sessions")
