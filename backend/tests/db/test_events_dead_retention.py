"""Exercise dead-letter migration upgrade and downgrade on real PostgreSQL."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

from avelis.db import get_engine, new_id

pytestmark = pytest.mark.db


def test_dead_retention_migration_backfills_only_dead_at_migration_time_and_downgrades(meta_db):
    path = Path(__file__).resolve().parents[2] / "src/avelis/migrations/versions/0024_event_outbox_dead_retention.py"
    spec = importlib.util.spec_from_file_location("migration_0024_events_dead_retention_db", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    # Isolate DDL from the migrated test database without changing its head.
    # The transaction rollback also removes this schema and its fixtures.
    schema = f"retention_{new_id()}"
    with get_engine().connect() as connection, connection.begin() as transaction:
        assert connection.scalar(text("SELECT current_database()")) == "avelis_m8_test"
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
        connection.exec_driver_sql("SET LOCAL TIME ZONE 'Pacific/Honolulu'")
        connection.exec_driver_sql(
            "CREATE TABLE event_outbox (id VARCHAR(32) PRIMARY KEY, status VARCHAR(16) NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO event_outbox (id, status) VALUES ('old-dead', 'dead'), "
            "('pending', 'pending'), ('delivered', 'delivered')"
        )
        timestamp = connection.scalar(text("SELECT CURRENT_TIMESTAMP AT TIME ZONE 'UTC'"))
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
        rows = dict(connection.execute(text("SELECT id, dead_at FROM event_outbox")).all())
        assert rows == {"old-dead": timestamp, "pending": None, "delivered": None}
        column = next(item for item in inspect(connection).get_columns("event_outbox") if item["name"] == "dead_at")
        assert column["nullable"] and column["default"] is None
        indexes = {item["name"]: item for item in inspect(connection).get_indexes("event_outbox")}
        assert indexes["ix_event_outbox_dead_cleanup"]["column_names"] == ["dead_at", "id"]
        with Operations.context(context):
            migration.downgrade()
        assert "dead_at" not in {item["name"] for item in inspect(connection).get_columns("event_outbox")}
        assert "ix_event_outbox_dead_cleanup" not in {
            item["name"] for item in inspect(connection).get_indexes("event_outbox")
        }
        assert connection.scalar(text("SELECT COUNT(*) FROM event_outbox")) == 3
        transaction.rollback()
