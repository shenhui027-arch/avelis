"""Outbox retention rules and bounded deletion without a live database."""

import importlib.util
import io
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from avelis.events import cleanup
from avelis.models import EventOutbox
from tests.unit.test_governance_events import database, subscription  # noqa: F401

NOW = datetime(2026, 11, 4)
SETTINGS = SimpleNamespace(events_delivered_retention_days=30)


def _row(identifier, *, status="delivered", delivered_at=None):
    return EventOutbox(
        id=identifier,
        event_id=identifier,
        subscription_id="sub",
        payload={},
        status=status,
        created_at=NOW - timedelta(days=100),
        next_attempt_at=NOW - timedelta(days=90),
        delivered_at=delivered_at,
    )


def test_cleanup_keeps_pending_dead_recent_deliveries_and_missing_terminal_timestamp(database):
    cutoff = NOW - timedelta(days=30)
    with database() as session:
        subscription(session)
        session.add_all(
            [
                _row("expired", delivered_at=cutoff - timedelta(seconds=1)),
                _row("boundary", delivered_at=cutoff),
                _row("recent", delivered_at=cutoff + timedelta(seconds=1)),
                _row("missing-time"),
                _row("pending", status="pending", delivered_at=cutoff - timedelta(days=1)),
                _row("dead", status="dead", delivered_at=cutoff - timedelta(days=1)),
            ]
        )
    with database() as session:
        assert cleanup.purge_batch(session, SETTINGS, NOW) == {"event_outbox": 1}
    with database() as session:
        assert set(session.scalars(select(EventOutbox.id))) == {"boundary", "recent", "missing-time", "pending", "dead"}


def test_cleanup_drains_oldest_delivered_snapshots_in_bounded_batches(database):
    with database() as session:
        subscription(session)
        session.add_all(
            [
                _row("third", delivered_at=NOW - timedelta(days=31)),
                _row("second", delivered_at=NOW - timedelta(days=32)),
                _row("first", delivered_at=NOW - timedelta(days=33)),
            ]
        )
    with database() as session:
        assert cleanup.purge_batch(session, SETTINGS, NOW, batch_size=2) == {"event_outbox": 2}
    with database() as session:
        assert list(session.scalars(select(EventOutbox.id))) == ["third"]
        assert cleanup.purge_batch(session, SETTINGS, NOW, batch_size=2) == {"event_outbox": 1}
    with database() as session:
        assert cleanup.purge_batch(session, SETTINGS, NOW, batch_size=2) == {"event_outbox": 0}


def test_disabled_cleanup_does_not_query_database():
    assert cleanup.purge_batch(None, SimpleNamespace(events_delivered_retention_days=0), NOW) == {"event_outbox": 0}
    assert cleanup.purge_batch(None, SETTINGS, NOW, batch_size=0) == {"event_outbox": 0}


def test_scheduled_cleanup_commits_one_bounded_batch(database, monkeypatch):
    with database() as session:
        subscription(session)
        session.add_all(_row(f"old-{index}", delivered_at=NOW - timedelta(days=31)) for index in range(501))
    monkeypatch.setattr(cleanup, "session_scope", database)
    monkeypatch.setattr(cleanup, "get_settings", lambda: SETTINGS)
    monkeypatch.setattr(cleanup, "utcnow", lambda: NOW)
    assert cleanup.purge() == {"event_outbox": 500}
    with database() as session:
        assert len(list(session.scalars(select(EventOutbox.id)))) == 1


def test_cleanup_statement_locks_only_bounded_expired_delivered_rows():
    sql = str(
        cleanup._expired_statement(NOW - timedelta(days=30), 500).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "event_outbox.status = 'delivered'" in sql
    assert "event_outbox.delivered_at <" in sql
    assert "ORDER BY event_outbox.delivered_at, event_outbox.id" in sql
    assert "LIMIT 500 FOR UPDATE SKIP LOCKED" in sql
    assert "JOIN" not in sql


def test_cleanup_index_migration_compiles_postgresql_upgrade_and_downgrade():
    path = Path(__file__).resolve().parents[2] / "src/avelis/migrations/versions/0023_event_outbox_cleanup_index.py"
    spec = importlib.util.spec_from_file_location("migration_0023_events_cleanup", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.revision == "0023_event_outbox_cleanup_index"
    assert migration.down_revision == "0022_oauth_cleanup_indexes"
    output = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    with Operations.context(context):
        migration.upgrade()
        migration.downgrade()
    sql = output.getvalue()
    assert (
        "CREATE INDEX ix_event_outbox_delivered_cleanup ON event_outbox (delivered_at, id) WHERE status = 'delivered'"
        in sql
    )
    assert "DROP INDEX ix_event_outbox_delivered_cleanup" in sql
