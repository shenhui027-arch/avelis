"""join path proposal source

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-31
"""

import sqlalchemy as sa
from alembic import op

from avelis.db import JsonType

revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('join_path', sa.Column('source', JsonType, nullable=True))


def downgrade() -> None:
    op.drop_column('join_path', 'source')
