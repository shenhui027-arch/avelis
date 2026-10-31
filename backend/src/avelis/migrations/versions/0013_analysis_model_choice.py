"""per-run analysis model choice

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-31
"""

import sqlalchemy as sa
from alembic import op

from avelis.db import JsonType

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('analysis_run', sa.Column('model_choice', JsonType, nullable=True))


def downgrade() -> None:
    op.drop_column('analysis_run', 'model_choice')
