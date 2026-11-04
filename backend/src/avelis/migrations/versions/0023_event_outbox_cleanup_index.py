"""Index delivered snapshots by their retention timestamp for bounded cleanup."""

import sqlalchemy as sa
from alembic import op

revision = "0023_event_outbox_cleanup_index"
down_revision = "0022_oauth_cleanup_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The due index uses next_attempt_at, which cannot bound delivered_at scans.
    # Pending and replayable dead rows do not need this retention-only index.
    op.create_index(
        "ix_event_outbox_delivered_cleanup",
        "event_outbox",
        ["delivered_at", "id"],
        postgresql_where=sa.text("status = 'delivered'"),
    )


def downgrade() -> None:
    op.drop_index("ix_event_outbox_delivered_cleanup", table_name="event_outbox")
