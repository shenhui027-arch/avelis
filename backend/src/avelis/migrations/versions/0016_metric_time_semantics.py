"""口径的时间与量纲语义

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-01
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("metric_version", sa.Column("time_format", sa.String(16), nullable=True))
    op.add_column("metric_version", sa.Column("data_grain", sa.String(8), server_default="day", nullable=False))
    op.add_column("metric_version", sa.Column("time_aggregation", sa.String(8), server_default="sum", nullable=False))
    op.add_column("metric_version", sa.Column("value_kind", sa.String(8), nullable=True))
    op.add_column("metric_version", sa.Column("polarity", sa.String(8), nullable=True))


def downgrade() -> None:
    for column in ("polarity", "value_kind", "time_aggregation", "data_grain", "time_format"):
        op.drop_column("metric_version", column)
