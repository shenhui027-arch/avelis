"""analysis context compaction request

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-31
"""

import sqlalchemy as sa
from alembic import op

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('analysis_run', sa.Column('compact_requested', sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column('analysis_run', 'compact_requested')
