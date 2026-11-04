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
SETTINGS = SimpleNamespace(events_delivered_retention_days=30, events_dead_retention_days=90)


def _row(identifier, *, status="delivered", delivered_at=None, dead_at=None):
    return EventOutbox(
        id=identifier,
        event_id=identifier,
        subscription_id="sub",
        payload={},
        status=status,
        created_at=NOW - timedelta(days=100),
        next_attempt_at=NOW - timedelta(days=90),
        delivered_at=delivered_at,
        dead_at=dead_at,
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


def test_cleanup_drains_both_states_with_independent_batch_limits_and_keeps_replayed_rows(database):
    cutoff = NOW - timedelta(days=90)
    with database() as session:
        subscription(session)
        session.add_all(
            [
                _row("delivered", delivered_at=NOW - timedelta(days=31)),
                _row("oldest-dead", status="dead", dead_at=cutoff - timedelta(days=2)),
                _row("expired-dead", status="dead", dead_at=cutoff - timedelta(days=1)),
                _row("last-dead", status="dead", dead_at=cutoff - timedelta(seconds=1)),
                _row("boundary-dead", status="dead", dead_at=cutoff),
                _row("recent-dead", status="dead", dead_at=cutoff + timedelta(seconds=1)),
                _row("missing-dead-time", status="dead"),
                _row("replayed", status="pending", dead_at=cutoff - timedelta(days=1)),
            ]
        )
    with database() as session:
        assert cleanup.purge_batch(session, SETTINGS, NOW, batch_size=2) == {"event_outbox": 3}
        assert session.get(EventOutbox, "delivered") is None
        assert session.get(EventOutbox, "oldest-dead") is None
        assert session.get(EventOutbox, "expired-dead") is None
        assert session.get(EventOutbox, "last-dead") is not None
    with database() as session:
        assert cleanup.purge_batch(session, SETTINGS, NOW, batch_size=2) == {"event_outbox": 1}
        assert set(session.scalars(select(EventOutbox.id))) == {
            "boundary-dead",
            "recent-dead",
            "missing-dead-time",
            "replayed",
        }


def test_each_retention_can_be_disabled_independently(database):
    with database() as session:
        subscription(session)
        session.add_all(
            [
                _row("delivered", delivered_at=NOW - timedelta(days=31)),
                _row("dead", status="dead", dead_at=NOW - timedelta(days=91)),
            ]
        )
    with database() as session:
        settings = SimpleNamespace(events_delivered_retention_days=0, events_dead_retention_days=90)
        assert cleanup.purge_batch(session, settings, NOW) == {"event_outbox": 1}
        assert session.get(EventOutbox, "delivered") is not None
    with database() as session:
        session.add(_row("dead", status="dead", dead_at=NOW - timedelta(days=91)))
    with database() as session:
        settings = SimpleNamespace(events_delivered_retention_days=30, events_dead_retention_days=0)
        assert cleanup.purge_batch(session, settings, NOW) == {"event_outbox": 1}
        assert session.get(EventOutbox, "dead") is not None


def test_disabled_cleanup_does_not_query_database():
    settings = SimpleNamespace(events_delivered_retention_days=0, events_dead_retention_days=0)
    assert cleanup.purge_batch(None, settings, NOW) == {"event_outbox": 0}
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


def test_cleanup_statement_uses_dead_age_and_skips_locked_rows():
    sql = str(
        cleanup._expired_statement(NOW - timedelta(days=90), 10, status="dead").compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "event_outbox.status = 'dead'" in sql
    assert "event_outbox.dead_at <" in sql
    assert "ORDER BY event_outbox.dead_at, event_outbox.id" in sql
    assert "LIMIT 10 FOR UPDATE SKIP LOCKED" in sql
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


def test_dead_retention_migration_compiles_postgresql_upgrade_and_downgrade():
    path = Path(__file__).resolve().parents[2] / "src/avelis/migrations/versions/0024_event_outbox_dead_retention.py"
    spec = importlib.util.spec_from_file_location("migration_0024_events_dead_retention", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.revision == "0024_event_outbox_dead_retention"
    assert migration.down_revision == "0023_event_outbox_cleanup_index"
    output = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    with Operations.context(context):
        migration.upgrade()
        migration.downgrade()
    sql = output.getvalue()
    assert "ALTER TABLE event_outbox ADD COLUMN dead_at TIMESTAMP WITHOUT TIME ZONE" in sql
    assert "UPDATE event_outbox SET dead_at = CURRENT_TIMESTAMP AT TIME ZONE 'UTC' WHERE status = 'dead'" in sql
    assert "CREATE INDEX ix_event_outbox_dead_cleanup ON event_outbox (dead_at, id) WHERE status = 'dead'" in sql
    assert "DROP INDEX ix_event_outbox_dead_cleanup" in sql
    assert "ALTER TABLE event_outbox DROP COLUMN dead_at" in sql
