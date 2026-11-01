"""对话迁移的 PostgreSQL DDL、存量链回填与根运行默认值。"""

import importlib.util
import io
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import JSON, Column, MetaData, String, Table, create_engine, inspect, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from avelis.db import Base
from avelis.models import AnalysisRun, AnalysisTask, User


def load_migration():
    path = Path(__file__).resolve().parents[2] / "src/avelis/migrations/versions/0016_analysis_conversation.py"
    spec = importlib.util.spec_from_file_location("migration_0016_conversation", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_conversation_migration_compiles_postgresql_upgrade_and_downgrade():
    migration = load_migration()
    assert migration.revision == "0016_conversation"
    assert migration.down_revision == "0015"
    ddl = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": ddl, "target_metadata": Base.metadata}
    )
    with Operations.context(context):
        migration.upgrade()
        migration.downgrade()
    sql = ddl.getvalue()
    assert "ADD COLUMN conversation_id VARCHAR(32)" in sql
    assert "ADD COLUMN display_title VARCHAR(80)" in sql
    assert "ADD COLUMN archived_at TIMESTAMP WITHOUT TIME ZONE" in sql
    assert "WITH RECURSIVE roots" in sql
    assert "ALTER COLUMN conversation_id SET NOT NULL" in sql
    assert "FOREIGN KEY(conversation_id) REFERENCES analysis_run (id)" in sql
    assert "CREATE INDEX ix_analysis_run_conversation_id" in sql
    assert "DROP COLUMN conversation_id" in sql
    assert "DROP COLUMN display_title" in sql
    assert "DROP COLUMN archived_at" in sql
    assert AnalysisRun.__table__.c.conversation_id.nullable is False
    assert AnalysisRun.__table__.c.display_title.nullable is True
    assert AnalysisRun.__table__.c.display_title.type.length == 80
    assert AnalysisRun.__table__.c.archived_at.nullable is True
    assert AnalysisRun.__table__.c.archived_at.type.impl.timezone is False


def test_conversation_migration_backfills_parent_chains_and_keeps_schedules_independent():
    migration = load_migration()
    engine = create_engine("sqlite://")
    metadata = MetaData()
    original = Table(
        "analysis_run",
        metadata,
        Column("id", String(32), primary_key=True),
        Column("parent_run_id", String(32)),
        Column("task_id", String(32)),
        Column("trigger", String(16), nullable=False),
        Column("question", String, nullable=False),
    )
    metadata.create_all(engine)
    rows = [
        {"id": "root", "parent_run_id": None, "task_id": None, "trigger": "manual", "question": "根问题"},
        {"id": "second", "parent_run_id": "root", "task_id": None, "trigger": "manual", "question": "继续查"},
        {"id": "third", "parent_run_id": "second", "task_id": None, "trigger": "manual", "question": "再追问"},
        {"id": "branch", "parent_run_id": "root", "task_id": None, "trigger": "manual", "question": "另一个追问"},
        {"id": "other", "parent_run_id": None, "task_id": None, "trigger": "manual", "question": "独立问题"},
        {"id": "schedule-1", "parent_run_id": None, "task_id": "task", "trigger": "schedule", "question": "定时问题"},
        {"id": "schedule-2", "parent_run_id": None, "task_id": "task", "trigger": "schedule", "question": "定时问题"},
    ]
    with engine.begin() as connection:
        connection.execute(original.insert(), rows)
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
        columns = {column["name"]: column for column in inspect(connection).get_columns("analysis_run")}
        assert columns["conversation_id"]["nullable"] is False
        assert columns["display_title"]["nullable"] is True
        assert columns["archived_at"]["nullable"] is True
        upgraded = Table("analysis_run", MetaData(), autoload_with=connection)
        result = {row["id"]: dict(row) for row in connection.execute(select(upgraded)).mappings()}
        for row in rows:
            root_id = "root" if row["id"] in {"root", "second", "third", "branch"} else row["id"]
            assert result[row["id"]] == {
                **row,
                "conversation_id": root_id,
                "display_title": None,
                "archived_at": None,
            }
        assert inspect(connection).get_indexes("analysis_run")[0]["column_names"] == ["conversation_id"]
        assert inspect(connection).get_foreign_keys("analysis_run")[0]["constrained_columns"] == ["conversation_id"]
        with Operations.context(context):
            migration.downgrade()
        restored = Table("analysis_run", MetaData(), autoload_with=connection)
        assert list(connection.execute(select(restored)).mappings()) == rows
    engine.dispose()


def test_conversation_migration_enforces_existing_root_reference():
    migration = load_migration()
    engine = create_engine("sqlite://")
    metadata = MetaData()
    Table("analysis_run", metadata, Column("id", String(32), primary_key=True), Column("parent_run_id", String(32)))
    metadata.create_all(engine)
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
        upgraded = Table("analysis_run", MetaData(), autoload_with=connection)
        connection.execute(upgraded.insert(), {"id": "root", "conversation_id": "root"})
        connection.execute(
            upgraded.insert(),
            {
                "id": "followup",
                "parent_run_id": "root",
                "conversation_id": "root",
            },
        )
        with pytest.raises(IntegrityError):
            connection.execute(upgraded.insert(), {"id": "invalid", "conversation_id": "missing"})
        with pytest.raises(IntegrityError):
            connection.execute(upgraded.insert(), {"id": "unset", "conversation_id": None})
    engine.dispose()


def test_root_run_defaults_conversation_to_explicit_or_generated_id():
    engine = create_engine("sqlite://")
    metadata = MetaData()
    for model in (User, AnalysisTask, AnalysisRun):
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(id="user", username="user", display_name="用户", password_hash="unused"))
        explicit = AnalysisRun(id="explicit", user_id="user", question="问题", trigger="manual", status="queued")
        generated = AnalysisRun(user_id="user", question="问题", trigger="manual", status="queued")
        session.add_all([explicit, generated])
        session.flush()
        assert explicit.conversation_id == explicit.id == "explicit"
        assert generated.conversation_id == generated.id
        assert explicit.archived_at is None
        assert generated.archived_at is None
        followup = AnalysisRun(
            user_id="user",
            parent_run_id=explicit.id,
            conversation_id=explicit.id,
            question="继续查",
            trigger="manual",
            status="queued",
        )
        session.add(followup)
        session.flush()
        assert followup.conversation_id == explicit.id
        assert followup.id != explicit.id
    engine.dispose()
