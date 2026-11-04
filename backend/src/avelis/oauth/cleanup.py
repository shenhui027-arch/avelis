"""Bounded OAuth garbage collection, run by the worker's periodic scan."""

import logging
from datetime import timedelta

from sqlalchemy import delete, or_, select, update

from avelis.config import get_settings
from avelis.db import session_scope
from avelis.models import OAuthAuthorizationCode, OAuthClient, OAuthConsentTicket, OAuthToken

from .limits import unapproved_clients
from .repository import now

log = logging.getLogger(__name__)


def purge_batch(session, settings, timestamp=None) -> dict[str, int]:
    """At most one batch per table per scan; no per-row audit writes."""
    timestamp = timestamp or now()
    batch = settings.mcp_cleanup_batch_size
    counts = {}
    stale = timestamp - timedelta(days=settings.mcp_pending_client_days)
    stale_clients = select(OAuthClient.id).where(
        OAuthClient.source == "dynamic", OAuthClient.created_at <= stale, unapproved_clients()
    )
    cutoff = timestamp - timedelta(days=settings.mcp_replay_retention_days)
    # Retain until BOTH credentials have expired and any later revocation is old.
    # A still-valid refresh token must survive even if its access token expired.
    expired_tokens = (
        OAuthToken.expires_at <= cutoff,
        or_(OAuthToken.refresh_expires_at.is_(None), OAuthToken.refresh_expires_at <= cutoff),
        or_(OAuthToken.access_revoked_at.is_(None), OAuthToken.access_revoked_at <= cutoff),
        or_(OAuthToken.refresh_revoked_at.is_(None), OAuthToken.refresh_revoked_at <= cutoff),
    )
    predicates = {
        # Drain tickets of expired registrations too, so repeatedly requesting
        # fresh tickets cannot keep an anonymous client alive indefinitely.
        OAuthConsentTicket: (
            or_(OAuthConsentTicket.expires_at <= timestamp, OAuthConsentTicket.client_record_id.in_(stale_clients)),
        ),
        OAuthAuthorizationCode: (OAuthAuthorizationCode.expires_at <= timestamp,),
        OAuthToken: expired_tokens,
    }
    candidates = {}
    client_ids = set()
    for model, predicate in predicates.items():
        rows = session.execute(
            select(model.id, model.client_record_id).where(*predicate).order_by(model.expires_at, model.id).limit(batch)
        ).all()
        candidates[model] = [row.id for row in rows]
        client_ids.update(row.client_record_id for row in rows)

    pending_ids = list(session.scalars(stale_clients.order_by(OAuthClient.created_at, OAuthClient.id).limit(batch)))
    client_ids.update(pending_ids)
    # Lock ALL clients first, in a single order, before touching any ticket/code/token.
    # Otherwise cleanup could hold a code lock while exchange holds its client lock.
    # Skip clients in use; recheck predicates under the locks on the next scan.
    locked = list(
        session.scalars(
            select(OAuthClient.id)
            .where(OAuthClient.id.in_(client_ids))
            .order_by(OAuthClient.id)
            .with_for_update(skip_locked=True)
        )
    )
    for model, predicate in predicates.items():
        ids = list(
            session.scalars(
                select(model.id).where(model.id.in_(candidates[model]), model.client_record_id.in_(locked), *predicate)
            )
        )
        if model is OAuthToken and ids:
            # Descendants may still be live. parent_token_id is lineage, not replay
            # lookup; replay detection uses credential hashes and family_id.
            session.execute(update(OAuthToken).where(OAuthToken.parent_token_id.in_(ids)).values(parent_token_id=None))
        if ids:
            session.execute(delete(model).where(model.id.in_(ids)))
        counts[model.__tablename__] = len(ids)

    clients = list(
        session.scalars(
            select(OAuthClient).where(OAuthClient.id.in_(pending_ids), OAuthClient.id.in_(locked), unapproved_clients())
        )
    )
    deleted = 0
    for client in clients:
        # A pending client can own many tickets. Keep deletion work bounded as well;
        # tickets are drained by the bounded ticket batch above.
        if session.scalar(
            select(OAuthConsentTicket.id).where(OAuthConsentTicket.client_record_id == client.id).limit(1)
        ):
            continue
        session.delete(client)
        deleted += 1
    session.flush()
    counts[OAuthClient.__tablename__] = deleted
    return counts


def purge() -> dict[str, int]:
    with session_scope() as session:
        counts = purge_batch(session, get_settings())
    log.info("OAuth cleanup: %s", counts)
    return counts
