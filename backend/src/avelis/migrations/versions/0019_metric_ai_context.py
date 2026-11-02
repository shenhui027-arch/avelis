"""口径级语义提示。

Revision ID: 0019_metric_ai_context
Revises: 0018_import_batches
"""

import sqlalchemy as sa
from alembic import op

from avelis.db import JsonType

revision = "0019_metric_ai_context"
down_revision = "0018_import_batches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("metric", sa.Column("ai_context", JsonType, nullable=False, server_default="{}"))


def downgrade() -> None:
    op.drop_column("metric", "ai_context")
