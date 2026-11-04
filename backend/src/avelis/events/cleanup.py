"""Bounded outbox retention, scheduled independently from history cleanup."""

import logging
from datetime import timedelta

from sqlalchemy import delete, select

from avelis.config import get_settings
from avelis.db import session_scope, utcnow
from avelis.models import EventOutbox

log = logging.getLogger(__name__)
BATCH_SIZE = 500


def _expired_statement(cutoff, batch_size, *, status="delivered"):
    terminal_at = EventOutbox.delivered_at if status == "delivered" else EventOutbox.dead_at
    return (
        select(EventOutbox.id)
        .where(EventOutbox.status == status, terminal_at < cutoff)
        .order_by(terminal_at, EventOutbox.id)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )


def purge_batch(session, settings, timestamp=None, *, batch_size=BATCH_SIZE) -> dict[str, int]:
    """Delete one bounded batch per terminal state using its retention timestamp.

    Separate ordered lookups use the two partial indexes without scanning pending
    rows. Each state has its own limit so neither can starve the other; both skip
    rows used by another worker.
    """
    if batch_size <= 0:
        return {"event_outbox": 0}
    timestamp = timestamp or utcnow()
    deleted = 0
    for status, days in (
        ("delivered", settings.events_delivered_retention_days),
        ("dead", settings.events_dead_retention_days),
    ):
        if days <= 0:
            continue
        cutoff = timestamp - timedelta(days=days)
        ids = list(session.scalars(_expired_statement(cutoff, batch_size, status=status)))
        if ids:
            session.execute(delete(EventOutbox).where(EventOutbox.id.in_(ids)))
            deleted += len(ids)
    return {"event_outbox": deleted}


def purge() -> dict[str, int]:
    with session_scope() as session:
        counts = purge_batch(session, get_settings())
    log.info("Event outbox cleanup: %s", counts)
    return counts
