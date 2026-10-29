"""derived metrics

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-29 23:30:00
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('metric_version', sa.Column('kind', sa.String(length=16), server_default='base', nullable=False))
    op.add_column('metric_version', sa.Column('components', postgresql.JSONB(astext_type=sa.Text()),
                                              server_default='[]', nullable=False))
    op.alter_column('metric_version', 'base_asset_id', existing_type=sa.String(length=32), nullable=True)


def downgrade() -> None:
    op.alter_column('metric_version', 'base_asset_id', existing_type=sa.String(length=32), nullable=False)
    op.drop_column('metric_version', 'components')
    op.drop_column('metric_version', 'kind')
