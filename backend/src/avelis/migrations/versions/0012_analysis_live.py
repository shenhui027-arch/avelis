"""analysis interjections and cooperative stop

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-31
"""

import sqlalchemy as sa
from alembic import op

from avelis.db import UTCDateTime

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('analysis_run', sa.Column('stop_requested', sa.Boolean(), server_default=sa.false(), nullable=False))
    op.create_table('analysis_message',
                    sa.Column('id', sa.String(32), primary_key=True),
                    sa.Column('run_id', sa.String(32), nullable=False),
                    sa.Column('user_id', sa.String(32), nullable=False),
                    sa.Column('text', sa.Text(), nullable=False),
                    sa.Column('created_at', UTCDateTime(), nullable=False),
                    sa.Column('consumed_at', UTCDateTime(), nullable=True),
                    sa.Column('consumed_seq', sa.Integer(), nullable=True),
                    sa.ForeignKeyConstraint(['run_id'], ['analysis_run.id']),
                    sa.ForeignKeyConstraint(['user_id'], ['app_user.id']))
    op.create_index('ix_analysis_message_run_created', 'analysis_message', ['run_id', 'created_at'])
    op.create_index(op.f('ix_analysis_message_user_id'), 'analysis_message', ['user_id'])


def downgrade() -> None:
    op.drop_table('analysis_message')
    op.drop_column('analysis_run', 'stop_requested')
