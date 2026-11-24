"""Add skills, model_choice, and reasoning_effort columns to analysis_task."""

import sqlalchemy as sa
from alembic import op

from avelis.db import JsonType

revision = "0028_analysis_task_options"
down_revision = "0027_auth_extensions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("analysis_task") as batch_op:
        batch_op.add_column(sa.Column("skills", JsonType, server_default="[]", nullable=False))
        batch_op.add_column(sa.Column("model_choice", JsonType, nullable=True))
        batch_op.add_column(sa.Column("reasoning_effort", sa.String(8), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("analysis_task") as batch_op:
        batch_op.drop_column("reasoning_effort")
        batch_op.drop_column("model_choice")
        batch_op.drop_column("skills")
