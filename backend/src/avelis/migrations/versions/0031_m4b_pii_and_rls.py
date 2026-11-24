"""Add asset_column_policy and asset_rls_policy tables for M4B data security."""

import sqlalchemy as sa
from alembic import op

from avelis.db import IdType, JsonType, UTCDateTime

revision = "0031_m4b_pii_and_rls"
down_revision = "0030_m3b_multi_model_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asset_column_policy",
        sa.Column("id", IdType, primary_key=True),
        sa.Column("asset_id", IdType, sa.ForeignKey("asset.id", ondelete="CASCADE"), nullable=False),
        sa.Column("column_name", sa.String(256), nullable=False),
        sa.Column("classification", sa.String(32), server_default="pii", nullable=False),
        sa.Column("masking_strategy", sa.String(16), server_default="mask", nullable=False),
        sa.Column("exempt_roles", JsonType, server_default="[]", nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("updated_by", IdType, sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("updated_at", UTCDateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.UniqueConstraint("asset_id", "column_name", name="uq_asset_column_policy_asset_col"),
    )
    op.create_index("ix_asset_column_policy_asset_id", "asset_column_policy", ["asset_id"])

    op.create_table(
        "asset_rls_policy",
        sa.Column("id", IdType, primary_key=True),
        sa.Column("asset_id", IdType, sa.ForeignKey("asset.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("scope_type", sa.String(16), server_default="all", nullable=False),
        sa.Column("scope_value", sa.String(128), nullable=True),
        sa.Column("predicate_sql", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("updated_by", IdType, sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("created_at", UTCDateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", UTCDateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )
    op.create_index("ix_asset_rls_asset_enabled", "asset_rls_policy", ["asset_id", "enabled"])


def downgrade() -> None:
    op.drop_index("ix_asset_rls_asset_enabled", table_name="asset_rls_policy")
    op.drop_table("asset_rls_policy")
    op.drop_index("ix_asset_column_policy_asset_id", table_name="asset_column_policy")
    op.drop_table("asset_column_policy")
