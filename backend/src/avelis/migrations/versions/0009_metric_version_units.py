"""metric version unit and unit_scale

口径版本新增可选的单位（ADR 0009 第 1 点）：unit（如 元、单、人）与 unit_scale（列本身的倍数，列已是“万元”时为 10000，
默认 1）。分析报告的格式器据此输出单位；缺单位的口径在治理页面列为待补。只影响显示，不计入依赖指纹。

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-30 12:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('metric_version', sa.Column('unit', sa.String(length=16), nullable=True))
    op.add_column('metric_version', sa.Column('unit_scale', sa.BigInteger(), server_default='1', nullable=False))


def downgrade() -> None:
    op.drop_column('metric_version', 'unit_scale')
    op.drop_column('metric_version', 'unit')
