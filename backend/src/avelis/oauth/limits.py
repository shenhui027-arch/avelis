"""Redis fixed windows and a database-serialized dynamic registration quota."""

import time

from authlib.oauth2.rfc6749 import OAuth2Error
from fastapi import Request
from redis.exceptions import RedisError
from sqlalchemy import exists, func, select, text

from avelis import audit, cache
from avelis.models import OAuthClient, OAuthGrant

from .repository import credential_hash


def window_count(action: str, source: str, seconds: int = 3600) -> tuple[int, int]:
    timestamp = int(time.time())
    bucket = timestamp // seconds
    name = f"avelis:oauth-limit:{action}:{credential_hash(source)}:{bucket}"
    # MULTI/EXEC prevents an INCR without its expiry, including on connection loss.
    with cache.client().pipeline(transaction=True) as pipe:
        pipe.incr(name)
        pipe.expire(name, seconds + 1, nx=True)
        count, _ = pipe.execute()
    return int(count), seconds - timestamp % seconds


def check_source(request: Request, session, action: str, limit: int):
    # uvicorn applies AVELIS_FORWARDED_ALLOW_IPS before populating request.client.
    # Never interpret forwarded headers again here.
    source = request.client.host if request.client else "-"
    try:
        count, retry = window_count(action, source)
    except RedisError:
        # Login throttling also refuses to proceed if its Redis check fails.
        return 503, {"error": "temporarily_unavailable"}, {"Retry-After": "60"}
    if count <= limit:
        return None
    if count == limit + 1:
        audit.record(session, None, "oauth.budget_exceeded", "oauth_client", None, reason=action, source=source)
    return 429, {"error": "temporarily_unavailable"}, {"Retry-After": str(retry)}


def unapproved_clients():
    # A revoked grant still proves that a user authorized this client in the past.
    return ~exists().where(OAuthGrant.client_record_id == OAuthClient.id)


def pending_quota_available(session, limit: int) -> bool:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('avelis:oauth-registration', 0))"))
    count = session.scalar(
        select(func.count()).select_from(OAuthClient).where(OAuthClient.source == "dynamic", unapproved_clients())
    )
    if count < limit:
        return True
    # One global summary per hour; repeated quota failures cannot grow the audit table.
    try:
        seen, _ = window_count("pending_quota", "global")
    except RedisError as exc:
        raise OAuth2Error(error="temporarily_unavailable", status_code=503) from exc
    if seen == 1:
        audit.record(
            session, None, "oauth.budget_exceeded", "oauth_client", None, reason="pending_clients", limit=limit
        )
    return False
