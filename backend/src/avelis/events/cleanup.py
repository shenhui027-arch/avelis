"""Bounded outbox retention, scheduled independently from history cleanup."""

import logging
from datetime import timedelta

from sqlalchemy import delete, select

from avelis.config import get_settings
from avelis.db import session_scope, utcnow
from avelis.models import EventOutbox

log = logging.getLogger(__name__)
BATCH_SIZE = 500


def _expired_statement(cutoff, batch_size):
    return (
        select(EventOutbox.id)
        .where(EventOutbox.status == "delivered", EventOutbox.delivered_at < cutoff)
        .order_by(EventOutbox.delivered_at, EventOutbox.id)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )


def purge_batch(session, settings, timestamp=None, *, batch_size=BATCH_SIZE) -> dict[str, int]:
    """Delete at most one batch; pending and replayable dead snapshots stay.

    A dead row has no reliable terminal timestamp, so neither its creation time
    nor its scheduled retry time can safely define a dead-letter retention age.
    Lock only the expired delivered rows and skip rows used by another worker.
    """
    days = settings.events_delivered_retention_days
    if days <= 0 or batch_size <= 0:
        return {"event_outbox": 0}
    timestamp = timestamp or utcnow()
    cutoff = timestamp - timedelta(days=days)
    ids = list(session.scalars(_expired_statement(cutoff, batch_size)))
    if ids:
        session.execute(delete(EventOutbox).where(EventOutbox.id.in_(ids)))
    return {"event_outbox": len(ids)}


def purge() -> dict[str, int]:
    with session_scope() as session:
        counts = purge_batch(session, get_settings())
    log.info("Event outbox cleanup: %s", counts)
    return counts
