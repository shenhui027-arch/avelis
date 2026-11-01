"""批量血缘导入与文件版本。

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa
from alembic import op

from avelis.db import JsonType, UTCDateTime

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    op.create_table(
        "import_batch",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("datasource_id", sa.String(32), sa.ForeignKey("datasource.id"), nullable=False),
        sa.Column("default_db", sa.String(128)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("progress", JsonType, nullable=False),
        sa.Column("stats", JsonType, nullable=False),
        sa.Column("options", JsonType, nullable=False),
        sa.Column("notes", JsonType, nullable=False),
        sa.Column("job_id", sa.String(32), sa.ForeignKey("job.id")),
        sa.Column("created_by", sa.String(32), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.Column("error", sa.Text()),
    )
    op.create_index("ix_import_batch_created", "import_batch", ["created_at"])
    op.add_column("import_job", sa.Column("batch_id", sa.String(32)))
    op.create_foreign_key("fk_import_job_batch_id_import_batch", "import_job", "import_batch", ["batch_id"], ["id"])
    op.create_index("ix_import_job_batch_id", "import_job", ["batch_id"])
    op.add_column("import_job", sa.Column("source_key", sa.String(512), nullable=False, server_default=""))
    op.add_column("import_job", sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("import_job", sa.Column("superseded_by", sa.String(32)))
    op.create_foreign_key("fk_import_job_superseded_by_import_job", "import_job", "import_job",
                          ["superseded_by"], ["id"])
    op.add_column("extracted_statement", sa.Column("text_hash", sa.String(64), nullable=False, server_default=""))
    op.add_column("extracted_statement", sa.Column("writes", JsonType, nullable=False, server_default="[]"))
    op.create_index("ix_statement_job_status", "extracted_statement", ["job_id", "status"])
    # 旧非 SQL 片段的 model 不是解析状态；待确认仍由候选表派生。
    connection.execute(sa.text("UPDATE extracted_statement SET status = 'unsupported' WHERE status = 'model'"))
    connection.execute(sa.text("""
        UPDATE import_job SET stats = jsonb_set(stats, '{statements}',
            ((stats->'statements') - 'model') || jsonb_build_object('unsupported',
                coalesce((stats->'statements'->>'unsupported')::integer, 0)
                + (stats->'statements'->>'model')::integer))
        WHERE jsonb_typeof(stats->'statements') = 'object' AND (stats->'statements') ? 'model'
    """))
    # 复用原文件 ID 作为独立表里的批次 ID；不用改历史原文、语句或哈希链上的边。
    connection.execute(sa.text("""
        INSERT INTO import_batch
            (id, datasource_id, default_db, status, progress, stats, options, notes,
             created_by, created_at, updated_at)
        SELECT id, datasource_id, default_db,
               CASE WHEN status IN ('needs_params', 'needs_review', 'done', 'failed') THEN status ELSE 'done' END,
               jsonb_build_object('files_total', 1, 'files_done', 1,
                   'statements_done', (SELECT count(*) FROM extracted_statement s WHERE s.job_id = j.id)),
               stats,
               jsonb_build_object('params', params, 'model_assist',
                   jsonb_build_object('failed_sql', false, 'code', false), 'sql_functions', '[]'::jsonb),
               '[]'::jsonb, created_by, created_at, updated_at
        FROM import_job j
    """))
    # 同名旧导入全部保留当前状态；最新记录沿用文件名，其余使用独立来源键。
    connection.execute(sa.text("""
        WITH ranked AS (
            SELECT id, row_number() OVER (
                PARTITION BY datasource_id, filename ORDER BY created_at DESC, id DESC
            ) AS position
            FROM import_job
        )
        UPDATE import_job j SET batch_id = j.id, revision = 1,
            source_key = CASE WHEN ranked.position = 1 THEN j.filename
                              ELSE j.filename || '#' || left(j.id, 8) END
        FROM ranked WHERE ranked.id = j.id
    """))
    # 规范化与运行时使用相同的方言规则，SQL 解析只在内存中进行。
    from avelis.lineage.sql import SqlDialect, canonical_hash_of
    from sqlglot.errors import SqlglotError

    rows = connection.execute(sa.text("""
        SELECT s.id, s.raw_text, s.sql_text, s.method, j.dialect
        FROM extracted_statement s JOIN import_job j ON j.id = s.job_id
    """)).mappings()
    from avelis.connectors.base import sha256_text

    for row in rows:
        text = row["raw_text"]
        try:
            digest = (sha256_text(text.strip()) if row["method"] == "unrecognized"
                      else canonical_hash_of(text, SqlDialect(row["dialect"], ""))[1])
        except (SqlglotError, ValueError, TypeError):
            digest = sha256_text(text.strip())
        connection.execute(sa.text("UPDATE extracted_statement SET text_hash = :digest WHERE id = :id"),
                           {"digest": digest, "id": row["id"]})
    op.drop_constraint("uq_import_job_datasource_id_content_hash", "import_job", type_="unique")
    op.create_index("ix_import_job_content", "import_job", ["datasource_id", "content_hash"])
    op.create_index("ix_import_job_current_source", "import_job", ["datasource_id", "source_key"],
                    unique=True, postgresql_where=sa.text("status <> 'superseded'"))


def downgrade() -> None:
    # 旧模型不能表示多个内容相同的历史版本；拒绝丢失留底数据的降级。
    duplicate = op.get_bind().execute(sa.text("""
        SELECT 1 FROM import_job GROUP BY datasource_id, content_hash HAVING count(*) > 1 LIMIT 1
    """)).first()
    if duplicate:
        raise RuntimeError("历史导入包含重复内容，旧模型无法保留这些版本；请先备份并明确处理方式")
    op.drop_index("ix_import_job_current_source", table_name="import_job")
    op.drop_index("ix_import_job_content", table_name="import_job")
    op.create_unique_constraint("uq_import_job_datasource_id_content_hash", "import_job",
                                ["datasource_id", "content_hash"])
    op.drop_index("ix_statement_job_status", table_name="extracted_statement")
    op.drop_column("extracted_statement", "writes")
    op.drop_column("extracted_statement", "text_hash")
    op.drop_constraint("fk_import_job_superseded_by_import_job", "import_job", type_="foreignkey")
    op.drop_column("import_job", "superseded_by")
    op.drop_column("import_job", "revision")
    op.drop_column("import_job", "source_key")
    op.drop_index("ix_import_job_batch_id", table_name="import_job")
    op.drop_constraint("fk_import_job_batch_id_import_batch", "import_job", type_="foreignkey")
    op.drop_column("import_job", "batch_id")
    op.drop_index("ix_import_batch_created", table_name="import_batch")
    op.drop_table("import_batch")
