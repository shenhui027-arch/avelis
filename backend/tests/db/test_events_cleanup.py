"""Real PostgreSQL outbox cleanup, delivery locking, and retention index plan."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Event
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, event, inspect, select, text

from avelis.db import get_engine, new_id, session_scope, utcnow
from avelis.events import cleanup, delivery
from avelis.models import EventOutbox, WebhookSubscription

pytestmark = pytest.mark.db
NOW = datetime(2001, 1, 1)
SETTINGS = SimpleNamespace(events_delivered_retention_days=30)
SYNC_TIMEOUT = 5


def _timeouts(connection):
    connection.exec_driver_sql("SET LOCAL lock_timeout = '1s'")
    connection.exec_driver_sql("SET LOCAL statement_timeout = '3s'")
    connection.exec_driver_sql("SET LOCAL idle_in_transaction_session_timeout = '10s'")


@pytest.fixture
def world(meta_db):
    with session_scope() as session:
        assert session.scalar(text("SELECT current_database()")) == "avelis_m8_test"
        enabled = list(session.scalars(select(WebhookSubscription).where(WebhookSubscription.enabled.is_(True))))
        previous_ids = [row.id for row in enabled]
        for row in enabled:
            row.enabled = False
        subscription = WebhookSubscription(
            name=f"retention-{new_id()}",
            url="https://8.8.8.8/retention",
            event_types=["collect.completed"],
            enabled=True,
            secret_enc="unused-offline-fixture",
        )
        session.add(subscription)
        session.flush()
        identifier = subscription.id
    engine = get_engine()
    event.listen(engine, "begin", _timeouts)
    try:
        yield identifier
    finally:
        event.remove(engine, "begin", _timeouts)
        with session_scope() as session:
            session.execute(delete(EventOutbox).where(EventOutbox.subscription_id == identifier))
            session.execute(delete(WebhookSubscription).where(WebhookSubscription.id == identifier))
            for row in session.scalars(select(WebhookSubscription).where(WebhookSubscription.id.in_(previous_ids))):
                row.enabled = True


def _row(subscription_id, *, status="delivered", delivered_at=None):
    return EventOutbox(
        id=new_id(),
        event_id=new_id(),
        subscription_id=subscription_id,
        payload={},
        status=status,
        attempts=1,
        created_at=NOW - timedelta(days=100),
        next_attempt_at=utcnow() - timedelta(minutes=1),
        delivered_at=delivered_at,
    )


def test_cleanup_skips_locked_expired_rows_and_preserves_pending_and_dead(world):
    with session_scope() as session:
        locked = _row(world, delivered_at=NOW - timedelta(days=32))
        free = _row(world, delivered_at=NOW - timedelta(days=31))
        pending, dead = _row(world, status="pending"), _row(world, status="dead")
        recent = _row(world, delivered_at=NOW - timedelta(days=1))
        session.add_all([locked, free, pending, dead, recent])
        ids = {row.id for row in (locked, free, pending, dead, recent)}
    with session_scope() as holder:
        holder.execute(select(EventOutbox.id).where(EventOutbox.id == locked.id).with_for_update()).one()
        with ThreadPoolExecutor(max_workers=1) as executor:

            def run_cleanup():
                with session_scope() as session:
                    session.execute(text("SET LOCAL lock_timeout = '1s'"))
                    return cleanup.purge_batch(session, SETTINGS, NOW)

            assert executor.submit(run_cleanup).result(timeout=SYNC_TIMEOUT) == {"event_outbox": 1}
        assert holder.get(EventOutbox, locked.id) is not None
    with session_scope() as session:
        remaining = set(session.scalars(select(EventOutbox.id).where(EventOutbox.subscription_id == world)))
        assert remaining == ids - {free.id}
        assert cleanup.purge_batch(session, SETTINGS, NOW) == {"event_outbox": 1}


def test_delivery_holding_pending_and_subscription_locks_does_not_block_cleanup(world, monkeypatch):
    with session_scope() as session:
        pending = _row(world, status="pending")
        expired = _row(world, delivered_at=NOW - timedelta(days=31))
        session.add_all([pending, expired])
    entered, release = Event(), Event()

    def receive(_subscription, _outbox, **_kwargs):
        entered.set()
        assert release.wait(SYNC_TIMEOUT), "cleanup did not finish while delivery held its row locks"

    monkeypatch.setattr(delivery, "send_webhook", receive)
    with ThreadPoolExecutor(max_workers=2) as executor:
        delivering = executor.submit(delivery.deliver_due, batch_size=1, budget_seconds=10)
        try:
            assert entered.wait(SYNC_TIMEOUT), "delivery did not claim the pending row"

            def run_cleanup():
                with session_scope() as session:
                    session.execute(text("SET LOCAL lock_timeout = '1s'"))
                    return cleanup.purge_batch(session, SETTINGS, NOW)

            assert executor.submit(run_cleanup).result(timeout=SYNC_TIMEOUT) == {"event_outbox": 1}
        finally:
            release.set()
        assert delivering.result(timeout=SYNC_TIMEOUT) == 1
    with session_scope() as session:
        assert session.get(EventOutbox, pending.id).status == "delivered"
        assert session.get(EventOutbox, expired.id) is None


def test_cleanup_holding_expired_row_lock_does_not_block_delivery(world, monkeypatch):
    with session_scope() as session:
        pending = _row(world, status="pending")
        session.add_all([pending, _row(world, delivered_at=NOW - timedelta(days=31))])
    sent = []
    monkeypatch.setattr(delivery, "send_webhook", lambda _subscription, outbox, **_kwargs: sent.append(outbox.id))
    with session_scope() as holder:
        assert cleanup.purge_batch(holder, SETTINGS, NOW) == {"event_outbox": 1}
        # Keep the deletion uncommitted until the separate delivery transaction completes.
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert (
                executor.submit(delivery.deliver_due, batch_size=1, budget_seconds=10).result(timeout=SYNC_TIMEOUT) == 1
            )
    assert sent == [pending.id]
    with session_scope() as session:
        assert session.get(EventOutbox, pending.id).status == "delivered"


def test_delivered_retention_index_bounds_expired_snapshot_lookup(world):
    indexes = {item["name"]: item for item in inspect(get_engine()).get_indexes("event_outbox")}
    index = indexes["ix_event_outbox_delivered_cleanup"]
    assert index["column_names"] == ["delivered_at", "id"]
    assert "delivered" in str(index["dialect_options"]["postgresql_where"])
    with session_scope() as session:
        # Few expired snapshots amid a much larger live/pending population. Let
        # PostgreSQL choose normally; do not force an index or disable seqscans.
        rows = [_row(world, status="pending") for _ in range(2000)]
        rows += [_row(world, delivered_at=NOW) for _ in range(2000)]
        rows += [_row(world, delivered_at=NOW - timedelta(days=31)) for _ in range(5)]
        session.add_all(rows)
        session.flush()
        session.execute(text("ANALYZE event_outbox"))
        statement = cleanup._expired_statement(NOW - timedelta(days=30), 500)
        sql = str(statement.compile(dialect=session.bind.dialect, compile_kwargs={"literal_binds": True}))
        plan = session.scalar(text(f"EXPLAIN (FORMAT JSON) {sql}"))[0]["Plan"]

        def index_names(node):
            return {node.get("Index Name")} | {name for child in node.get("Plans", []) for name in index_names(child)}

        assert "ix_event_outbox_delivered_cleanup" in index_names(plan), plan


def test_budget_deferred_delivery_is_claimed_before_fresh_rows_with_null_errors(world):
    """PostgreSQL 的 DESC 默认 NULLS FIRST：被调度推迟的记录必须排在 last_error 为空的新记录之前。"""
    with session_scope() as session:
        fresh = [_row(world, status="pending") for _ in range(3)]
        for index, row in enumerate(fresh):
            row.next_attempt_at = utcnow() - timedelta(minutes=10 - index)
        deferred = _row(world, status="pending")
        deferred.last_error = delivery.BUDGET_TIMEOUT
        session.add_all([*fresh, deferred])
        session.flush()
        expected = deferred.id
    with session_scope() as session:
        outbox, _subscription = session.execute(delivery._due_statement(utcnow(), set())).first()
        assert outbox.id == expected
