"""Cowork 长期记忆。

RAG 的 memory 做的是时序有效性建模（ADR-0005），是知识层的东西；Cowork 需要的是
"用户偏好和项目约定"这类轻量事实，两者不共用一张表，也不共用一套语义——`rag ⊥ cowork`
（ADR-0011）本来就不允许直接复用。

`forgotten_at` 是软删除，客户端的撤销和模型误删后的恢复都依赖它。

Revision ID: 20260820_0035
Revises: 20260820_0034
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260820_0035"
down_revision: str | None = "20260820_0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cowork_memories",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("workspace_path", sa.Text(), nullable=True),
        sa.Column("key", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False, server_default="agent"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("forgotten_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "scope IN ('global','workspace','conversation')",
            name="ck_cowork_memories_scope",
        ),
        sa.CheckConstraint("source IN ('agent','user')", name="ck_cowork_memories_source"),
        # 作用域和它的定位字段必须自洽：global 不绑任何东西，workspace 绑规范化目录，
        # conversation 绑会话。写歪了会让检索悄悄漏掉或串到别的作用域。
        sa.CheckConstraint(
            "(scope = 'global' AND conversation_id IS NULL AND workspace_path IS NULL)"
            " OR (scope = 'workspace' AND conversation_id IS NULL AND workspace_path IS NOT NULL)"
            " OR (scope = 'conversation' AND conversation_id IS NOT NULL"
            " AND workspace_path IS NULL)",
            name="ck_cowork_memories_scope_binding",
        ),
        sa.CheckConstraint(
            "char_length(content) BETWEEN 1 AND 4000",
            name="ck_cowork_memories_content_length",
        ),
    )
    op.create_index(
        "ix_cowork_memories_active",
        "cowork_memories",
        ["scope", "updated_at"],
        postgresql_where=sa.text("forgotten_at IS NULL"),
    )
    # 同一作用域内 key 是幂等句柄："更新而不是堆一条新的"靠它落地。NULL 在唯一索引里
    # 互不相等，所以用 COALESCE 折叠，而不是依赖 NULLS NOT DISTINCT。
    op.execute(
        """
        CREATE UNIQUE INDEX uq_cowork_memories_key ON cowork_memories (
            scope,
            COALESCE(conversation_id, '00000000-0000-0000-0000-000000000000'::uuid),
            COALESCE(workspace_path, ''),
            key
        ) WHERE key IS NOT NULL AND forgotten_at IS NULL
        """
    )


def downgrade() -> None:
    op.drop_index("uq_cowork_memories_key", table_name="cowork_memories")
    op.drop_index("ix_cowork_memories_active", table_name="cowork_memories")
    op.drop_table("cowork_memories")
