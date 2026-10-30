"""metric version material_change_ratio

口径版本新增可选的变化显著性阈值 material_change_ratio（ADR 0009 第 7 点）：|本期 − 对比期| 低于对比期的这个比例时，
explain_change 判定“总体变化不明显”，报告的结论类型随之确定。为空时用系统默认（AVELIS_MATERIAL_CHANGE_RATIO，1%）。
只影响分析判断，不改变查询语义，不计入依赖指纹。

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-30 20:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('metric_version', sa.Column('material_change_ratio', sa.Numeric(8, 6), nullable=True))


def downgrade() -> None:
    op.drop_column('metric_version', 'material_change_ratio')
