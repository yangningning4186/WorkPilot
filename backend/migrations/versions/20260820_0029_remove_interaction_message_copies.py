"""Remove ask_user answers accidentally copied into the chat transcript.

Revision ID: 20260820_0029
Revises: 20260820_0028
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260820_0029"
down_revision: str | None = "20260820_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The old interaction endpoint inserted the answer immediately after setting
    # responded_at. PostgreSQL's transaction-stable now() gives both rows the
    # same timestamp, which lets us remove only those implementation-detail
    # copies while preserving ordinary user and steering messages.
    op.execute(
        """
        DELETE FROM messages AS message
        USING cowork_inbox_items AS inbox
        WHERE message.run_id = inbox.run_id
          AND message.role = 'user'
          AND inbox.kind = 'ask_user'
          AND inbox.status = 'answered'
          AND inbox.responded_at IS NOT NULL
          AND message.created_at = inbox.responded_at
          AND BTRIM(message.content) = BTRIM(inbox.response->>'answer')
        """
    )


def downgrade() -> None:
    # The canonical answer remains in cowork_inbox_items and the checkpoint tool
    # result. Recreating the erroneous chat copy on downgrade would reintroduce
    # the UI bug, so this data cleanup is intentionally irreversible.
    pass
