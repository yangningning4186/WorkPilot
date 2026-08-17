"""评测事实组与等价证据。

Revision ID: 20260818_0018
Revises: 20260817_0017
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260818_0018"
down_revision: str | None = "20260817_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "eval_items",
        sa.Column(
            "gold_evidence_groups",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    # 兼容迁移：每个旧 span 先成为一个独立事实组。后续人工审计只需给组补 alternatives。
    op.execute(
        """
        UPDATE eval_items i
        SET gold_evidence_groups = COALESCE(
            (
                SELECT jsonb_agg(
                    jsonb_build_object(
                        'fact_id', 'R' || ordinality::text,
                        'alternatives', jsonb_build_array(span)
                    )
                    ORDER BY ordinality
                )
                FROM jsonb_array_elements(i.gold_spans)
                     WITH ORDINALITY AS entry(span, ordinality)
            ),
            '[]'::jsonb
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION validate_eval_evidence_groups(groups JSONB)
        RETURNS BOOLEAN
        LANGUAGE SQL
        STABLE
        PARALLEL SAFE
        AS $$
            SELECT CASE
                WHEN groups IS NULL OR jsonb_typeof(groups) <> 'array' THEN false
                ELSE COALESCE(
                    (
                        SELECT bool_and(
                            jsonb_typeof(fact) = 'object'
                            AND length(COALESCE(fact->>'fact_id', '')) > 0
                            AND jsonb_typeof(fact->'alternatives') = 'array'
                            AND jsonb_array_length(fact->'alternatives') > 0
                            AND validate_eval_spans(fact->'alternatives')
                        )
                        AND count(*) = count(DISTINCT fact->>'fact_id')
                        FROM jsonb_array_elements(groups) AS fact
                    ),
                    true
                )
            END
        $$
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION validate_eval_evidence_groups(JSONB)")
    op.drop_column("eval_items", "gold_evidence_groups")
