"""0025 lineage_note：修订链、PostgreSQL DDL、建出的列与模型一致。"""

import importlib.util
import io
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from avelis.models import LineageNote

VERSIONS = Path(__file__).resolve().parents[2] / "src/avelis/migrations/versions"


def load_migration():
    spec = importlib.util.spec_from_file_location("migration_0025", VERSIONS / "0025_lineage_note.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_revision_chain():
    migration = load_migration()
    assert migration.revision == "0025_lineage_note"
    assert migration.down_revision == "0024_event_outbox_dead_retention"
    script = ScriptDirectory(str(VERSIONS.parent))
    assert script.get_revision(migration.revision).down_revision == migration.down_revision
    assert script.get_revision(migration.down_revision) is not None


def test_postgresql_ddl():
    migration = load_migration()
    ddl = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": ddl})
    with Operations.context(context):
        migration.upgrade()
        migration.downgrade()
    sql = ddl.getvalue()
    assert "CREATE TABLE lineage_note" in sql
    assert "UNIQUE (asset_id, column_name)" in sql
    assert "DROP TABLE lineage_note" in sql


def test_created_columns_match_model_and_roundtrip():
    migration = load_migration()
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            columns = {c["name"]: c for c in inspect(connection).get_columns("lineage_note")}
            model = LineageNote.__table__
            assert set(columns) == {c.name for c in model.columns}
            for column in model.columns:
                assert columns[column.name]["nullable"] == column.nullable, column.name
            uniques = inspect(connection).get_unique_constraints("lineage_note")
            assert [u["column_names"] for u in uniques] == [["asset_id", "column_name"]]
            migration.downgrade()
            assert "lineage_note" not in inspect(connection).get_table_names()
    engine.dispose()
