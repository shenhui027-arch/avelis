"""Indexes for bounded OAuth cleanup and client pagination."""

from alembic import op

revision = "0022_oauth_cleanup_indexes"
down_revision = "0021_mcp_oauth"
branch_labels = None
depends_on = None

INDEXES = {
    "oauth_consent_ticket": ["expires_at", "id"],
    "oauth_authorization_code": ["expires_at", "id"],
    "oauth_token": ["expires_at", "id"],
    "oauth_client": ["source", "created_at", "id"],
}


def upgrade() -> None:
    for table, columns in INDEXES.items():
        op.create_index(f"ix_{table}_cleanup", table, columns)
    op.create_index("ix_oauth_client_created_page", "oauth_client", ["created_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_oauth_client_created_page", table_name="oauth_client")
    for table in reversed(INDEXES):
        op.drop_index(f"ix_{table}_cleanup", table_name=table)
