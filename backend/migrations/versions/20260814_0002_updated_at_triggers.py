"""由数据库统一维护 updated_at。

Revision ID: 20260814_0002
Revises: 20260814_0001
Create Date: 2026-08-14
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260814_0002"
down_revision: str | None = "20260814_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPDATED_TABLES = (
    "sources",
    "documents",
    "document_versions",
    "conversations",
    "agent_runs",
    "eval_datasets",
    "eval_items",
    "daily_cost_budgets",
    "cost_reservations",
)


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION set_updated_at() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table_name in UPDATED_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_updated_at
            BEFORE UPDATE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION set_updated_at()
            """
        )


def downgrade() -> None:
    for table_name in reversed(UPDATED_TABLES):
        op.execute(f"DROP TRIGGER trg_{table_name}_updated_at ON {table_name}")
    op.execute("DROP FUNCTION set_updated_at()")
