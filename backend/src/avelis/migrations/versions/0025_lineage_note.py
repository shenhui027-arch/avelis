"""Asset dossier: plain-language notes for a table and its field lineage (ADR 0022)."""

import sqlalchemy as sa
from alembic import op

from avelis.db import UTCDateTime

revision = "0025_lineage_note"
down_revision = "0024_event_outbox_dead_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lineage_note",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("asset_id", sa.String(32), nullable=False),
        sa.Column("column_name", sa.String(255), server_default="", nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("basis_hash", sa.String(64), nullable=True),
        sa.Column("model_adapter", sa.String(64), nullable=True),
        sa.Column("created_by", sa.String(32), nullable=True),
        sa.Column("confirmed_by", sa.String(32), nullable=True),
        sa.Column("confirmed_at", UTCDateTime(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["asset.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["app_user.id"]),
        sa.ForeignKeyConstraint(["confirmed_by"], ["app_user.id"]),
        sa.UniqueConstraint("asset_id", "column_name"),
    )


def downgrade() -> None:
    op.drop_table("lineage_note")
