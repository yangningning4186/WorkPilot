"""增加 gold span 数据库校验函数与标注列表索引。

Revision ID: 20260814_0006
Revises: 20260814_0005
Create Date: 2026-08-14
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260814_0006"
down_revision: str | None = "20260814_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION validate_eval_spans(spans JSONB)
        RETURNS BOOLEAN
        LANGUAGE SQL
        STABLE
        PARALLEL SAFE
        AS $$
            SELECT CASE
                WHEN spans IS NULL OR jsonb_typeof(spans) <> 'array' THEN false
                ELSE COALESCE(
                    (
                        SELECT bool_and(
                            CASE
                                WHEN jsonb_typeof(span) <> 'object'
                                  OR COALESCE(span->>'version_id', '') !~
                                     '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
                                  OR COALESCE(span->>'char_start', '') !~ '^[0-9]+$'
                                  OR COALESCE(span->>'char_end', '') !~ '^[0-9]+$'
                                  OR span->>'quote' IS NULL
                                THEN false
                                ELSE EXISTS (
                                    SELECT 1
                                    FROM document_versions v
                                    WHERE v.id = (span->>'version_id')::uuid
                                      AND (span->>'char_end')::int >
                                          (span->>'char_start')::int
                                      AND substring(
                                          v.full_text
                                          FROM (span->>'char_start')::int + 1
                                          FOR (span->>'char_end')::int -
                                              (span->>'char_start')::int
                                      ) = span->>'quote'
                                )
                            END
                        )
                        FROM jsonb_array_elements(spans) AS span
                    ),
                    true
                )
            END
        $$
        """
    )
    op.create_index(
        "idx_eval_items_dataset_updated",
        "eval_items",
        ["dataset_id", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_eval_items_dataset_updated", table_name="eval_items")
    op.execute("DROP FUNCTION validate_eval_spans(JSONB)")
