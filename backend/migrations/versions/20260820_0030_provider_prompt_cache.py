"""Add Provider Prompt Cache token telemetry.

Revision ID: 20260820_0030
Revises: 20260820_0029
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260820_0030"
down_revision: str | None = "20260820_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "llm_calls",
        sa.Column(
            "prompt_cache_read_tokens",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "llm_calls",
        sa.Column(
            "prompt_cache_write_tokens",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "ck_llm_calls_prompt_cache_tokens",
        "llm_calls",
        "prompt_cache_read_tokens >= 0 AND prompt_cache_write_tokens >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_llm_calls_prompt_cache_tokens", "llm_calls", type_="check"
    )
    op.drop_column("llm_calls", "prompt_cache_write_tokens")
    op.drop_column("llm_calls", "prompt_cache_read_tokens")
