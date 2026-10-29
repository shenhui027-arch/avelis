"""history integrity: append-only triggers, hash chain, change attribution (ADR 0008)

Revision ID: 0007
Revises: 0005
Create Date: 2026-10-30 10:00:00

M8a 的迁移 0006 合并后，本迁移的 down_revision 改为 '0006'（谁后合并谁调整）。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from avelis.history import integrity

revision = '0007'
down_revision = '0005'
branch_labels = None
depends_on = None

CHAINED_EXISTING = ('asset_version', 'column_version', 'definition', 'lineage_edge', 'change_set', 'change_event',
                    'audit_event', 'metric_version_transition')


def _chain_columns(table: str) -> None:
    op.add_column(table, sa.Column('chain_seq', sa.BigInteger(), nullable=True))
    op.add_column(table, sa.Column('prev_hash', sa.String(length=64), nullable=True))
    op.add_column(table, sa.Column('row_hash', sa.String(length=64), nullable=True))
    op.create_unique_constraint(f'uq_{table}_chain_seq', table, ['chain_seq'])
    # 插入它的顶层事务号（xid8）：由插入触发器写入、不可改；未封链的行只有插入它的事务能补全
    op.execute(f'ALTER TABLE {table} ADD COLUMN inserted_xact xid8')


def upgrade() -> None:
    for table in CHAINED_EXISTING:
        _chain_columns(table)
    op.add_column('change_event', sa.Column('actor_db_user', sa.String(length=128), nullable=True))
    op.add_column('change_event', sa.Column('attribution_status', sa.String(length=16), server_default='unsupported',
                                            nullable=False))
    op.add_column('change_event', sa.Column('attribution', postgresql.JSONB(astext_type=sa.Text()),
                                            server_default='{}', nullable=False))
    op.create_index('ix_change_event_actor_db_user', 'change_event', ['actor_db_user'])
    op.create_table(
        'history_chain_head',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('last_seq', sa.BigInteger(), nullable=False),
        sa.Column('last_hash', sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint('id', name='pk_history_chain_head'),
    )
    op.create_table(
        'history_mark',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('table_name', sa.String(length=64), nullable=False),
        sa.Column('row_id', sa.String(length=32), nullable=False),
        sa.Column('column_name', sa.String(length=64), nullable=False),
        sa.Column('old_value', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('new_value', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.Column('chain_seq', sa.BigInteger(), nullable=True),
        sa.Column('prev_hash', sa.String(length=64), nullable=True),
        sa.Column('row_hash', sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint('id', name='pk_history_mark'),
        sa.UniqueConstraint('chain_seq', name='uq_history_mark_chain_seq'),
    )
    op.execute('ALTER TABLE history_mark ADD COLUMN inserted_xact xid8')
    op.create_index('ix_history_mark_row', 'history_mark', ['table_name', 'row_id'])
    op.create_table(
        'history_anchor',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.Column('cutoff', sa.DateTime(), nullable=False),
        sa.Column('retention_days', sa.Integer(), nullable=False),
        sa.Column('counts', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('gaps', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('actor_name', sa.String(length=64), nullable=False),
        sa.Column('chain_seq', sa.BigInteger(), nullable=True),
        sa.Column('prev_hash', sa.String(length=64), nullable=True),
        sa.Column('row_hash', sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint('id', name='pk_history_anchor'),
        sa.UniqueConstraint('chain_seq', name='uq_history_anchor_chain_seq'),
    )
    op.execute('ALTER TABLE history_anchor ADD COLUMN inserted_xact xid8')
    # 先为已有数据补算哈希，再启用触发器
    conn = op.get_bind()
    integrity.backfill(conn)
    integrity.install(conn)


def downgrade() -> None:
    integrity.run_raw(op.get_bind(), "".join(integrity.drop_sql(t) for t in integrity.TABLES))
    op.execute("DROP FUNCTION IF EXISTS avelis_history_seal(); DROP FUNCTION IF EXISTS avelis_history_guard(); "
               "DROP FUNCTION IF EXISTS avelis_history_insert();")
    op.drop_table('history_anchor')
    op.drop_index('ix_history_mark_row', table_name='history_mark')
    op.drop_table('history_mark')
    op.drop_table('history_chain_head')
    op.drop_index('ix_change_event_actor_db_user', table_name='change_event')
    for column in ('attribution', 'attribution_status', 'actor_db_user'):
        op.drop_column('change_event', column)
    for table in CHAINED_EXISTING:
        op.drop_constraint(f'uq_{table}_chain_seq', table, type_='unique')
        for column in ('inserted_xact', 'row_hash', 'prev_hash', 'chain_seq'):
            op.drop_column(table, column)
