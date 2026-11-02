"""MCP OAuth 客户端、授权、确认票据、授权码及令牌。

Revision ID: 0021_mcp_oauth
Revises: 0019_metric_ai_context
"""

import sqlalchemy as sa
from alembic import op

from avelis.db import JsonType, UTCDateTime

revision = "0021_mcp_oauth"
down_revision = "0020_governance_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oauth_client",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("client_id", sa.String(2048), unique=True, nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("client_name", sa.String(256), nullable=False),
        sa.Column("redirect_uris", JsonType, nullable=False),
        sa.Column("scope", sa.String(256), nullable=False),
        sa.Column("grant_types", JsonType, nullable=False),
        sa.Column("response_types", JsonType, nullable=False),
        sa.Column("token_endpoint_auth_method", sa.String(32), nullable=False),
        sa.Column("secret_hash", sa.String(64)),
        sa.Column("secret_expires_at", UTCDateTime()),
        sa.Column("metadata_hash", sa.String(64)),
        sa.Column("metadata_fetched_at", UTCDateTime()),
        sa.Column("metadata_expires_at", UTCDateTime()),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("last_used_at", UTCDateTime()),
        sa.Column("disabled_at", UTCDateTime()),
    )
    op.create_table(
        "oauth_grant",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("client_record_id", sa.String(32), sa.ForeignKey("oauth_client.id"), nullable=False),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("scope", sa.String(256), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("last_used_at", UTCDateTime()),
        sa.Column("revoked_at", UTCDateTime()),
    )
    op.create_table(
        "oauth_consent_ticket",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("ticket_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("client_record_id", sa.String(32), sa.ForeignKey("oauth_client.id"), nullable=False),
        sa.Column("parameters", JsonType, nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("expires_at", UTCDateTime(), nullable=False),
        sa.Column("consumed_at", UTCDateTime()),
    )
    op.create_table(
        "oauth_authorization_code",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("code_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("client_record_id", sa.String(32), sa.ForeignKey("oauth_client.id"), nullable=False),
        sa.Column("client_id", sa.String(2048), nullable=False),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("grant_id", sa.String(32), sa.ForeignKey("oauth_grant.id"), nullable=False),
        sa.Column("redirect_uri", sa.String(2048), nullable=False),
        sa.Column("scope", sa.String(256), nullable=False),
        sa.Column("resource", sa.String(2048), nullable=False),
        sa.Column("code_challenge", sa.String(128), nullable=False),
        sa.Column("code_challenge_method", sa.String(8), nullable=False),
        sa.Column("family_id", sa.String(32), nullable=False),
        sa.Column("issued_at", UTCDateTime(), nullable=False),
        sa.Column("expires_at", UTCDateTime(), nullable=False),
        sa.Column("consumed_at", UTCDateTime()),
    )
    op.create_table(
        "oauth_token",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("access_token_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("refresh_token_hash", sa.String(64), unique=True),
        sa.Column("client_record_id", sa.String(32), sa.ForeignKey("oauth_client.id"), nullable=False),
        sa.Column("client_id", sa.String(2048), nullable=False),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("grant_id", sa.String(32), sa.ForeignKey("oauth_grant.id"), nullable=False),
        sa.Column("family_id", sa.String(32), nullable=False),
        sa.Column("parent_token_id", sa.String(32), sa.ForeignKey("oauth_token.id")),
        sa.Column("scope", sa.String(256), nullable=False),
        sa.Column("resource", sa.String(2048), nullable=False),
        sa.Column("issued_at", UTCDateTime(), nullable=False),
        sa.Column("expires_at", UTCDateTime(), nullable=False),
        sa.Column("refresh_expires_at", UTCDateTime()),
        sa.Column("access_revoked_at", UTCDateTime()),
        sa.Column("refresh_revoked_at", UTCDateTime()),
        sa.Column("revocation_reason", sa.String(32)),
    )
    for table, columns in {
        "oauth_grant": ["client_record_id", "user_id"],
        "oauth_consent_ticket": ["client_record_id"],
        "oauth_authorization_code": ["client_record_id", "user_id", "grant_id", "family_id"],
        "oauth_token": ["client_record_id", "user_id", "grant_id", "family_id", "parent_token_id"],
    }.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade() -> None:
    for table in ("oauth_token", "oauth_authorization_code", "oauth_consent_ticket", "oauth_grant", "oauth_client"):
        op.drop_table(table)
