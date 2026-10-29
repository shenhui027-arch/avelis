"""user sessions and password policy

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-29 21:30:00
"""

import sqlalchemy as sa
from alembic import op

import avelis.db

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('app_user', sa.Column('must_change_password', sa.Boolean(), server_default=sa.false(),
                                        nullable=False))
    op.add_column('app_user', sa.Column('password_changed_at', avelis.db.UTCDateTime(), nullable=True))
    op.add_column('app_user', sa.Column('last_login_at', avelis.db.UTCDateTime(), nullable=True))
    op.add_column('app_user', sa.Column('sessions_valid_after', avelis.db.UTCDateTime(), nullable=True))


def downgrade() -> None:
    for column in ('sessions_valid_after', 'last_login_at', 'password_changed_at', 'must_change_password'):
        op.drop_column('app_user', column)
