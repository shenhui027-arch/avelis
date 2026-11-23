"""User auth extensions (OIDC, ForwardAuth, TOTP 2FA) and Personal Access Tokens."""

import sqlalchemy as sa
from alembic import op

from avelis.db import IdType, JsonType, UTCDateTime

revision = "0027_auth_extensions"
down_revision = "0026_skills"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("app_user") as batch_op:
        batch_op.add_column(sa.Column("email", sa.String(255), nullable=True))
        batch_op.add_column(sa.Column("auth_provider", sa.String(32), server_default="local", nullable=False))
        batch_op.add_column(sa.Column("external_issuer", sa.String(512), nullable=True))
        batch_op.add_column(sa.Column("external_sub", sa.String(255), nullable=True))
        batch_op.add_column(sa.Column("totp_secret_enc", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("totp_pending_secret_enc", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("totp_enabled", sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.add_column(sa.Column("totp_enabled_at", UTCDateTime(), nullable=True))
        batch_op.add_column(sa.Column("totp_last_step", sa.BigInteger(), nullable=True))
        batch_op.create_unique_constraint("uq_app_user_external_identity", ["external_issuer", "external_sub"])
        batch_op.create_index("ix_app_user_email", ["email"])

    op.create_table(
        "personal_access_token",
        sa.Column("id", IdType, primary_key=True),
        sa.Column("user_id", IdType, sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("token_prefix", sa.String(16), nullable=False),
        sa.Column("token_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("scopes", JsonType, server_default="[]", nullable=False),
        sa.Column("expires_at", UTCDateTime(), nullable=True),
        sa.Column("last_used_at", UTCDateTime(), nullable=True),
        sa.Column("revoked_at", UTCDateTime(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
    )
    op.create_index("ix_personal_access_token_user_id", "personal_access_token", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_personal_access_token_user_id", table_name="personal_access_token")
    op.drop_table("personal_access_token")
    with op.batch_alter_table("app_user") as batch_op:
        batch_op.drop_index("ix_app_user_email")
        batch_op.drop_constraint("uq_app_user_external_identity", type_="unique")
        batch_op.drop_column("totp_last_step")
        batch_op.drop_column("totp_enabled_at")
        batch_op.drop_column("totp_enabled")
        batch_op.drop_column("totp_pending_secret_enc")
        batch_op.drop_column("totp_secret_enc")
        batch_op.drop_column("external_sub")
        batch_op.drop_column("external_issuer")
        batch_op.drop_column("auth_provider")
        batch_op.drop_column("email")
