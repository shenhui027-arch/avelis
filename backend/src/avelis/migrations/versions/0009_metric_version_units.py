"""metric version unit and unit_scale; domain example questions

口径版本新增可选的单位（ADR 0009 第 1 点）：unit（如 元、单、人）与 unit_scale（列本身的倍数，列已是“万元”时为 10000，
默认 1）。分析报告的格式器据此输出单位；缺单位的口径在治理页面列为待补。只影响显示，不计入依赖指纹。
业务域新增示例问题 example_questions（ADR 0010 第 3 点）：[{text, mode}]，问数页、分析页从接口读取。

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-30 12:00:00
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('metric_version', sa.Column('unit', sa.String(length=16), nullable=True))
    op.add_column('metric_version', sa.Column('unit_scale', sa.BigInteger(), server_default='1', nullable=False))
    op.add_column('domain', sa.Column('example_questions', postgresql.JSONB(astext_type=sa.Text()),
                                      server_default='[]', nullable=False))


def downgrade() -> None:
    op.drop_column('domain', 'example_questions')
    op.drop_column('metric_version', 'unit_scale')
    op.drop_column('metric_version', 'unit')
