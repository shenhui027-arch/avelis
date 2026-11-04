"""Record dead-letter age and index bounded dead-letter retention cleanup."""

import sqlalchemy as sa
from alembic import op

from avelis.db import UTCDateTime

revision = "0024_event_outbox_dead_retention"
down_revision = "0023_event_outbox_cleanup_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("event_outbox", sa.Column("dead_at", UTCDateTime(), nullable=True))
    # Old rows have no reliable death time. Start their full retention window at
    # migration time instead of expiring old snapshots immediately on upgrade.
    op.execute("UPDATE event_outbox SET dead_at = CURRENT_TIMESTAMP AT TIME ZONE 'UTC' WHERE status = 'dead'")
    op.create_index(
        "ix_event_outbox_dead_cleanup",
        "event_outbox",
        ["dead_at", "id"],
        postgresql_where=sa.text("status = 'dead'"),
    )


def downgrade() -> None:
    op.drop_index("ix_event_outbox_dead_cleanup", table_name="event_outbox")
    op.drop_column("event_outbox", "dead_at")
