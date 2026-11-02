"""关联提议来源迁移：PostgreSQL DDL 与 SQLite 通用升降级路径。"""

import importlib.util
import io
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import JSON, Column, MetaData, String, Table, create_engine, inspect, select, update
from sqlalchemy.dialects.postgresql import JSONB

from avelis.db import Base
from avelis.models import JoinPath


def load_migration():
    path = Path(__file__).resolve().parents[2] / "src/avelis/migrations/versions/0015_join_path_source.py"
    spec = importlib.util.spec_from_file_location("migration_0015", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_join_source_migration_compiles_postgresql_upgrade_and_downgrade():
    migration = load_migration()
    assert migration.revision == "0015"
    assert migration.down_revision == "0014"
    ddl = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": ddl, "target_metadata": Base.metadata}
    )
    with Operations.context(context):
        migration.upgrade()
        migration.downgrade()
    assert "ALTER TABLE join_path ADD COLUMN source JSONB" in ddl.getvalue()
    assert "ALTER TABLE join_path DROP COLUMN source" in ddl.getvalue()
    assert JoinPath.__table__.c.source.nullable is True
    assert isinstance(JoinPath.__table__.c.source.type, JSONB)


def test_join_source_migration_preserves_rows_and_json_on_sqlite(monkeypatch):
    migration = load_migration()
    # SQLite 验证通用 DDL 与数据保留；PostgreSQL 的 JSONB 由上面的编译测试验证。
    monkeypatch.setattr(migration, "JsonType", JSON())
    engine = create_engine("sqlite://")
    metadata = MetaData()
    original = Table(
        "join_path",
        metadata,
        Column("id", String(32), primary_key=True),
        Column("code", String(64), nullable=False),
        Column("status", String(16), nullable=False),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(original.insert(), {"id": "existing", "code": "existing_path", "status": "approved"})
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
        columns = {column["name"]: column for column in inspect(connection).get_columns("join_path")}
        assert columns["source"]["nullable"] is True
        assert isinstance(columns["source"]["type"], JSON)
        upgraded = Table("join_path", MetaData(), autoload_with=connection)
        assert connection.execute(select(upgraded)).mappings().one() == {
            "id": "existing",
            "code": "existing_path",
            "status": "approved",
            "source": None,
        }
        source = {"run_id": "run-1", "checkpoint": "proposal", "text": "按标识关联"}
        connection.execute(update(upgraded).values(source=source))
        assert connection.scalar(select(upgraded.c.source)) == source
        with Operations.context(context):
            migration.downgrade()
        assert "source" not in {column["name"] for column in inspect(connection).get_columns("join_path")}
        restored = Table("join_path", MetaData(), autoload_with=connection)
        assert connection.execute(select(restored)).mappings().one() == {
            "id": "existing",
            "code": "existing_path",
            "status": "approved",
        }
    engine.dispose()
