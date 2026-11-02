"""0019 的 PostgreSQL DDL 与 SQLite 存量数据 upgrade/downgrade 往返。"""

import importlib.util
import io
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import JSON, Column, MetaData, String, Table, create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError

from avelis.models import Metric

VERSIONS = Path(__file__).resolve().parents[2] / "src/avelis/migrations/versions"


def load_migration():
    spec = importlib.util.spec_from_file_location("migration_0019", VERSIONS / "0019_metric_ai_context.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_postgresql_ddl_and_revision_chain():
    migration = load_migration()
    assert migration.revision == "0019_metric_ai_context"
    assert migration.down_revision == "0018_import_batches"
    scripts = ScriptDirectory(str(VERSIONS.parent))
    assert scripts.get_revision("0021_mcp_oauth").down_revision == migration.revision
    assert scripts.get_current_head() == "0021_mcp_oauth"
    ddl = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": ddl})
    with Operations.context(context):
        migration.upgrade()
        migration.downgrade()
    assert "ADD COLUMN ai_context JSONB DEFAULT '{}' NOT NULL" in ddl.getvalue()
    assert "DROP COLUMN ai_context" in ddl.getvalue()
    assert Metric.__table__.c.ai_context.nullable is False
    assert Metric.__table__.c.ai_context.default.arg.__name__ == "dict"
    assert Metric.__table__.c.ai_context.server_default.arg == "{}"


def test_existing_metrics_backfilled_and_upgrade_downgrade_roundtrip(monkeypatch):
    migration = load_migration()
    # SQLite 用 JSON 替代 PostgreSQL JSONB，生产类型另由上面的 DDL 测试核对。
    monkeypatch.setattr(migration, "JsonType", JSON())
    engine = create_engine("sqlite://")
    metadata = MetaData()
    original = Table("metric", metadata, Column("id", String, primary_key=True), Column("name", String))
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(original.insert(), {"id": "old", "name": "指标甲"})
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            upgraded = Table("metric", MetaData(), autoload_with=connection)
            assert connection.execute(select(upgraded.c.ai_context)).scalar_one() == {}
            connection.execute(text("INSERT INTO metric(id, name) VALUES ('new', '指标乙')"))
            assert connection.execute(select(upgraded.c.ai_context).where(upgraded.c.id == "new")).scalar_one() == {}
            with pytest.raises(IntegrityError):
                connection.execute(text("UPDATE metric SET ai_context = NULL WHERE id = 'old'"))
            connection.execute(
                upgraded.update().where(upgraded.c.id == "old").values(ai_context={"synonyms": ["词甲"]})
            )
            migration.downgrade()
            assert {c["name"] for c in inspect(connection).get_columns("metric")} == {"id", "name"}
            assert connection.execute(select(original.c.name).order_by(original.c.id)).scalars().all() == [
                "指标乙",
                "指标甲",
            ]
            migration.upgrade()
            assert connection.execute(select(upgraded.c.ai_context)).scalars().all() == [{}, {}]
    engine.dispose()
