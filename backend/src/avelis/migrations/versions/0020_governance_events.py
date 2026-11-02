"""治理事件订阅与事务发件箱。

Revision ID: 0020_governance_events
Revises: 0019_metric_ai_context

接在 0019_metric_ai_context（口径语义提示）之后。
不修改 audit_event 的列或历史链摘要，避免改变既有历史行的哈希。
"""

import sqlalchemy as sa
from alembic import op

from avelis.db import JsonType, UTCDateTime

revision = "0020_governance_events"
down_revision = "0019_metric_ai_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webhook_subscription",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("event_types", JsonType, nullable=False, server_default="[]"),
        sa.Column("outcomes", JsonType),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("secret_enc", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(32), sa.ForeignKey("app_user.id")),
        sa.Column("created_at", UTCDateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", UTCDateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_webhook_subscription_created_by", "webhook_subscription", ["created_by"])
    op.create_table(
        "event_outbox",
        sa.Column("id", sa.String(32), primary_key=True),
        # 稳定事件 ID 不加 audit_event 外键；审计按保留期清理时，投递快照仍可独立重试。
        sa.Column("event_id", sa.String(32), nullable=False),
        sa.Column(
            "subscription_id", sa.String(32), sa.ForeignKey("webhook_subscription.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("payload", JsonType, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", UTCDateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", UTCDateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("delivered_at", UTCDateTime()),
        sa.UniqueConstraint("subscription_id", "event_id"),
        sa.CheckConstraint("status IN ('pending', 'delivered', 'dead')", name="status"),
        sa.CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
    )
    op.create_index("ix_event_outbox_subscription_id", "event_outbox", ["subscription_id"])
    op.create_index("ix_event_outbox_due", "event_outbox", ["status", "next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_event_outbox_due", table_name="event_outbox")
    op.drop_index("ix_event_outbox_subscription_id", table_name="event_outbox")
    op.drop_table("event_outbox")
    op.drop_index("ix_webhook_subscription_created_by", table_name="webhook_subscription")
    op.drop_table("webhook_subscription")
