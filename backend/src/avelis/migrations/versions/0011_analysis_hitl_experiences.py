"""analysis hypothesis checkpoints and personal experiences (ADR 0013, first version)

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-31
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from avelis.db import UTCDateTime

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('analysis_run', sa.Column('parent_run_id', sa.String(32), nullable=True))
    op.create_foreign_key(op.f('fk_analysis_run_parent_run_id_analysis_run'), 'analysis_run', 'analysis_run',
                          ['parent_run_id'], ['id'])
    op.create_index(op.f('ix_analysis_run_parent_run_id'), 'analysis_run', ['parent_run_id'])
    op.add_column('analysis_run', sa.Column('confirmation_mode', sa.String(8), server_default='auto', nullable=False))
    op.add_column('analysis_run', sa.Column('checkpoint', postgresql.JSONB(), server_default='{}', nullable=False))
    op.create_table('experience',
                    sa.Column('id', sa.String(32), primary_key=True),
                    sa.Column('owner_id', sa.String(32), nullable=False),
                    sa.Column('scope', sa.String(16), server_default='personal', nullable=False),
                    sa.Column('current_version', sa.Integer(), server_default='1', nullable=False),
                    sa.Column('status', sa.String(16), server_default='active', nullable=False),
                    sa.Column('created_at', UTCDateTime(), nullable=False),
                    sa.ForeignKeyConstraint(['owner_id'], ['app_user.id']))
    op.create_index('ix_experience_owner_status', 'experience', ['owner_id', 'status'])
    op.create_table('experience_version',
                    sa.Column('id', sa.String(32), primary_key=True),
                    sa.Column('experience_id', sa.String(32), nullable=False),
                    sa.Column('version', sa.Integer(), nullable=False),
                    sa.Column('kind', sa.String(16), nullable=False),
                    sa.Column('content', sa.Text(), nullable=False),
                    sa.Column('refs', postgresql.JSONB(), nullable=False),
                    sa.Column('source', postgresql.JSONB(), nullable=False),
                    sa.Column('confirmed_by', sa.String(32), nullable=False),
                    sa.Column('confirmed_at', UTCDateTime(), nullable=False),
                    sa.Column('superseded_by', sa.String(32), nullable=True),
                    sa.ForeignKeyConstraint(['experience_id'], ['experience.id']),
                    sa.ForeignKeyConstraint(['confirmed_by'], ['app_user.id']),
                    sa.ForeignKeyConstraint(['superseded_by'], ['experience_version.id']),
                    sa.UniqueConstraint('experience_id', 'version'))
    op.create_index(op.f('ix_experience_version_experience_id'), 'experience_version', ['experience_id'])
    op.create_index(op.f('ix_experience_version_superseded_by'), 'experience_version', ['superseded_by'])


def downgrade() -> None:
    op.drop_table('experience_version')
    op.drop_table('experience')
    op.drop_column('analysis_run', 'checkpoint')
    op.drop_column('analysis_run', 'confirmation_mode')
    op.drop_index(op.f('ix_analysis_run_parent_run_id'), table_name='analysis_run')
    op.drop_constraint(op.f('fk_analysis_run_parent_run_id_analysis_run'), 'analysis_run', type_='foreignkey')
    op.drop_column('analysis_run', 'parent_run_id')
