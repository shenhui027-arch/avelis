"""Versioned skill packages, review and per-user enablement."""

import sqlalchemy as sa
from alembic import op

from avelis.db import IdType, JsonType, UTCDateTime

revision = "0026_skills"
down_revision = "0025_lineage_note"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skill",
        sa.Column("id", IdType, primary_key=True),
        sa.Column("name", sa.String(64), unique=True, nullable=False),
        sa.Column("owner_id", IdType, sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("published_version", sa.Integer()),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
    )
    op.create_index("ix_skill_owner_id", "skill", ["owner_id"])
    op.create_table(
        "skill_version",
        sa.Column("id", IdType, primary_key=True),
        sa.Column("skill_id", IdType, sa.ForeignKey("skill.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("frontmatter", JsonType, nullable=False),
        sa.Column("files", JsonType, nullable=False),
        sa.Column("has_scripts", sa.Boolean(), nullable=False),
        sa.Column("line_count", sa.Integer(), nullable=False),
        sa.Column("zip_sha256", sa.String(64), nullable=False),
        sa.Column("zip_bytes", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("uploaded_by", IdType, sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("uploaded_at", UTCDateTime(), nullable=False),
        sa.Column("reviewed_by", IdType, sa.ForeignKey("app_user.id")),
        sa.Column("reviewed_at", UTCDateTime()),
        sa.Column("review_note", sa.Text()),
        sa.UniqueConstraint("skill_id", "version"),
    )
    op.create_index(
        "uq_skill_version_pending",
        "skill_version",
        ["skill_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "skill_enablement",
        sa.Column("user_id", IdType, sa.ForeignKey("app_user.id"), primary_key=True),
        sa.Column("skill_id", IdType, sa.ForeignKey("skill.id"), primary_key=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
    )
    op.create_index("ix_skill_enablement_skill_id", "skill_enablement", ["skill_id"])


def downgrade() -> None:
    op.drop_table("skill_enablement")
    op.drop_table("skill_version")
    op.drop_table("skill")
