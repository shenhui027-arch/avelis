"""analysis conversations

Revision ID: 0016_conversation
Revises: 0015
Create Date: 2026-11-01
"""

import sqlalchemy as sa
from alembic import op

from avelis.db import IdType

revision = '0016_conversation'
down_revision = '0015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('analysis_run', sa.Column('conversation_id', IdType, nullable=True))
    op.add_column('analysis_run', sa.Column('display_title', sa.String(80), nullable=True))
    op.add_column('analysis_run', sa.Column('archived_at', sa.DateTime(timezone=False), nullable=True))
    # 按父子链传播根 ID；同一 task 的定时运行没有 parent，仍各自独立。
    op.execute(sa.text("""
        WITH RECURSIVE roots (id, conversation_id) AS (
            SELECT id, id FROM analysis_run WHERE parent_run_id IS NULL
            UNION ALL
            SELECT child.id, roots.conversation_id
            FROM analysis_run AS child
            JOIN roots ON child.parent_run_id = roots.id
        )
        UPDATE analysis_run
        SET conversation_id = (
            SELECT roots.conversation_id FROM roots WHERE roots.id = analysis_run.id
        )
    """))
    with op.batch_alter_table('analysis_run') as batch_op:
        batch_op.alter_column('conversation_id', existing_type=IdType, nullable=False)
        batch_op.create_foreign_key('fk_analysis_run_conversation_id_analysis_run',
                                    'analysis_run', ['conversation_id'], ['id'])
        batch_op.create_index('ix_analysis_run_conversation_id', ['conversation_id'])


def downgrade() -> None:
    with op.batch_alter_table('analysis_run') as batch_op:
        batch_op.drop_index('ix_analysis_run_conversation_id')
        batch_op.drop_constraint('fk_analysis_run_conversation_id_analysis_run', type_='foreignkey')
        batch_op.drop_column('archived_at')
        batch_op.drop_column('display_title')
        batch_op.drop_column('conversation_id')
