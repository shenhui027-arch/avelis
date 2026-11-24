"""Enforce case-insensitive uniqueness on app_user.username after explicit conflict detection."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0032_username_ci_unique"
down_revision = "0031_m4b_pii_and_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not op.get_context().as_sql:
        conn = op.get_bind()
        duplicates = conn.execute(
            sa.text(
                "SELECT lower(username) AS norm_name "
                "FROM app_user "
                "GROUP BY lower(username) "
                "HAVING COUNT(*) > 1 "
                "ORDER BY norm_name"
            )
        ).fetchall()
        if duplicates:
            details: list[str] = []
            for (norm_name,) in duplicates:
                rows = conn.execute(
                    sa.text(
                        "SELECT id, username, auth_provider "
                        "FROM app_user "
                        "WHERE lower(username) = :norm "
                        "ORDER BY created_at, id"
                    ),
                    {"norm": norm_name},
                ).fetchall()
                accounts = ", ".join(f"(id={r[0]}, username={r[1]!r}, provider={r[2]})" for r in rows)
                details.append(f"  - {norm_name!r}: {accounts}")
            raise RuntimeError(
                "检测到仅大小写不同的存量用户名冲突，无法建立不区分大小写的唯一索引。"
                "请先人工确认并处理或重命名以下冲突账号后再执行迁移：\n" + "\n".join(details)
            )
    op.create_index("uq_app_user_username_lower", "app_user", [sa.text("lower(username)")], unique=True)


def downgrade() -> None:
    op.drop_index("uq_app_user_username_lower", table_name="app_user")
