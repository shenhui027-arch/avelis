"""Add ModelAssignment.backups, llm_usage_ledger, llm_pricing, and llm_quota."""

import sqlalchemy as sa
from alembic import op

from avelis.db import IdType, JsonType, UTCDateTime

revision = "0030_m3b_multi_model_ledger"
down_revision = "0029_vqr_domain_exp_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("model_assignment") as batch_op:
        batch_op.add_column(sa.Column("backups", JsonType, server_default="[]", nullable=False))

    op.create_table(
        "llm_usage_ledger",
        sa.Column("id", IdType, primary_key=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("user_id", IdType, sa.ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("domain_id", IdType, nullable=True),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("phase", sa.String(32), nullable=True),
        sa.Column("provider_id", IdType, nullable=True),
        sa.Column("provider_code", sa.String(64), nullable=True),
        sa.Column("model_id", sa.String(128), nullable=False),
        sa.Column("adapter", sa.String(192), nullable=True),
        sa.Column("thread_id", IdType, nullable=True),
        sa.Column("answer_id", IdType, nullable=True),
        sa.Column("run_id", IdType, nullable=True),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cache_hit_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reasoning_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("estimated_cost", sa.Numeric(14, 6), server_default="0", nullable=False),
        sa.Column("currency", sa.String(8), server_default="USD", nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), server_default="ok", nullable=False),
        sa.Column("failover_from", sa.String(192), nullable=True),
        sa.Column("routing_tier", sa.String(16), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
    )
    op.create_index("ix_llm_usage_ledger_created_at", "llm_usage_ledger", ["created_at"])
    op.create_index("ix_llm_usage_ledger_user_id", "llm_usage_ledger", ["user_id"])
    op.create_index("ix_llm_usage_ledger_domain_id", "llm_usage_ledger", ["domain_id"])
    op.create_index("ix_llm_usage_ledger_provider_id", "llm_usage_ledger", ["provider_id"])
    op.create_index("ix_llm_usage_ledger_model_id", "llm_usage_ledger", ["model_id"])
    op.create_index("ix_llm_usage_ledger_thread_id", "llm_usage_ledger", ["thread_id"])
    op.create_index("ix_llm_usage_ledger_answer_id", "llm_usage_ledger", ["answer_id"])
    op.create_index("ix_llm_usage_ledger_run_id", "llm_usage_ledger", ["run_id"])
    op.create_index("ix_llm_usage_user_created", "llm_usage_ledger", ["user_id", "created_at"])
    op.create_index("ix_llm_usage_domain_created", "llm_usage_ledger", ["domain_id", "created_at"])

    op.create_table(
        "llm_pricing",
        sa.Column("id", IdType, primary_key=True),
        sa.Column("provider_code", sa.String(64), server_default="*", nullable=False),
        sa.Column("model_id", sa.String(128), nullable=False),
        sa.Column("input_per_1m", sa.Numeric(14, 6), server_default="0", nullable=False),
        sa.Column("output_per_1m", sa.Numeric(14, 6), server_default="0", nullable=False),
        sa.Column("cache_hit_per_1m", sa.Numeric(14, 6), nullable=True),
        sa.Column("currency", sa.String(8), server_default="USD", nullable=False),
        sa.Column("updated_by", IdType, nullable=True),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("provider_code", "model_id", name="uq_llm_pricing_provider_model"),
    )

    op.create_table(
        "llm_quota",
        sa.Column("id", IdType, primary_key=True),
        sa.Column("scope_type", sa.String(16), nullable=False),
        sa.Column("scope_id", sa.String(64), nullable=False),
        sa.Column("daily_token_limit", sa.BigInteger(), nullable=True),
        sa.Column("monthly_token_limit", sa.BigInteger(), nullable=True),
        sa.Column("daily_cost_limit", sa.Numeric(14, 6), nullable=True),
        sa.Column("monthly_cost_limit", sa.Numeric(14, 6), nullable=True),
        sa.Column("rpm_limit", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("updated_by", IdType, nullable=True),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("scope_type", "scope_id", name="uq_llm_quota_scope"),
    )


def downgrade() -> None:
    op.drop_table("llm_quota")
    op.drop_table("llm_pricing")

    op.drop_index("ix_llm_usage_domain_created", table_name="llm_usage_ledger")
    op.drop_index("ix_llm_usage_user_created", table_name="llm_usage_ledger")
    op.drop_index("ix_llm_usage_ledger_run_id", table_name="llm_usage_ledger")
    op.drop_index("ix_llm_usage_ledger_answer_id", table_name="llm_usage_ledger")
    op.drop_index("ix_llm_usage_ledger_thread_id", table_name="llm_usage_ledger")
    op.drop_index("ix_llm_usage_ledger_model_id", table_name="llm_usage_ledger")
    op.drop_index("ix_llm_usage_ledger_provider_id", table_name="llm_usage_ledger")
    op.drop_index("ix_llm_usage_ledger_domain_id", table_name="llm_usage_ledger")
    op.drop_index("ix_llm_usage_ledger_user_id", table_name="llm_usage_ledger")
    op.drop_index("ix_llm_usage_ledger_created_at", table_name="llm_usage_ledger")
    op.drop_table("llm_usage_ledger")

    with op.batch_alter_table("model_assignment") as batch_op:
        batch_op.drop_column("backups")
