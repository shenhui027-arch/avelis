"""Add VerifiedQuery table, domain experience review columns, and Answer feedback columns."""

import sqlalchemy as sa
from alembic import op

from avelis.db import IdType, JsonType, LongText, UTCDateTime

revision = "0029_vqr_domain_exp_feedback"
down_revision = "0028_analysis_task_options"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "verified_query",
        sa.Column("id", IdType, primary_key=True),
        sa.Column("domain_id", IdType, sa.ForeignKey("domain.id"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("question_variants", JsonType, server_default="[]", nullable=False),
        sa.Column("sql_text", LongText, nullable=False),
        sa.Column("dialect", sa.String(32), server_default="postgres", nullable=False),
        sa.Column("datasource_id", IdType, sa.ForeignKey("datasource.id"), nullable=True),
        sa.Column("metric_codes", JsonType, server_default="[]", nullable=False),
        sa.Column("tags", JsonType, server_default="[]", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("source_answer_id", IdType, sa.ForeignKey("answer.id"), nullable=True),
        sa.Column("source_run_id", IdType, sa.ForeignKey("analysis_run.id"), nullable=True),
        sa.Column("status", sa.String(16), server_default="draft", nullable=False),
        sa.Column("created_by", IdType, sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.Column("submitted_by", IdType, sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("submitted_at", UTCDateTime(), nullable=True),
        sa.Column("reviewed_by", IdType, sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("reviewed_at", UTCDateTime(), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("approved_at", UTCDateTime(), nullable=True),
    )
    op.create_index("ix_verified_query_domain_status", "verified_query", ["domain_id", "status"])

    with op.batch_alter_table("experience") as batch_op:
        batch_op.add_column(sa.Column("domain_id", IdType, nullable=True))
        batch_op.add_column(sa.Column("review_status", sa.String(16), server_default="none", nullable=False))
        batch_op.add_column(sa.Column("submitted_by", IdType, nullable=True))
        batch_op.add_column(sa.Column("submitted_at", UTCDateTime(), nullable=True))
        batch_op.add_column(sa.Column("reviewed_by", IdType, nullable=True))
        batch_op.add_column(sa.Column("reviewed_at", UTCDateTime(), nullable=True))
        batch_op.add_column(sa.Column("review_note", sa.Text(), nullable=True))
        batch_op.create_foreign_key("fk_experience_domain_id_domain", "domain", ["domain_id"], ["id"])
        batch_op.create_foreign_key("fk_experience_submitted_by_app_user", "app_user", ["submitted_by"], ["id"])
        batch_op.create_foreign_key("fk_experience_reviewed_by_app_user", "app_user", ["reviewed_by"], ["id"])
        batch_op.create_index("ix_experience_domain_review", ["scope", "domain_id", "status", "review_status"])

    with op.batch_alter_table("answer") as batch_op:
        batch_op.add_column(sa.Column("feedback_rating", sa.String(8), nullable=True))
        batch_op.add_column(sa.Column("feedback_note", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("feedback_at", UTCDateTime(), nullable=True))
        batch_op.add_column(sa.Column("feedback_by", IdType, nullable=True))
        batch_op.create_foreign_key("fk_answer_feedback_by_app_user", "app_user", ["feedback_by"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table("answer") as batch_op:
        batch_op.drop_constraint("fk_answer_feedback_by_app_user", type_="foreignkey")
        batch_op.drop_column("feedback_by")
        batch_op.drop_column("feedback_at")
        batch_op.drop_column("feedback_note")
        batch_op.drop_column("feedback_rating")

    with op.batch_alter_table("experience") as batch_op:
        batch_op.drop_index("ix_experience_domain_review")
        batch_op.drop_constraint("fk_experience_reviewed_by_app_user", type_="foreignkey")
        batch_op.drop_constraint("fk_experience_submitted_by_app_user", type_="foreignkey")
        batch_op.drop_constraint("fk_experience_domain_id_domain", type_="foreignkey")
        batch_op.drop_column("review_note")
        batch_op.drop_column("reviewed_at")
        batch_op.drop_column("reviewed_by")
        batch_op.drop_column("submitted_at")
        batch_op.drop_column("submitted_by")
        batch_op.drop_column("review_status")
        batch_op.drop_column("domain_id")

    op.drop_index("ix_verified_query_domain_status", table_name="verified_query")
    op.drop_table("verified_query")
