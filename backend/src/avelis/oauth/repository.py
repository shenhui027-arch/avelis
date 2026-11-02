"""OAuth 数据库边界；请求事务提交后才对外返回凭据。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from authlib.oauth2.rfc6749 import InvalidGrantError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from avelis import audit
from avelis.db import new_id
from avelis.models import OAuthAuthorizationCode, OAuthClient, OAuthGrant, OAuthToken, User

SCOPES = ("metrics:read", "lineage:read", "ask:execute")


def credential_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def now() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class Repository:
    def __init__(self, session: Session):
        self.session = session

    def get_client(self, client_id: str, *, lock: bool = False) -> OAuthClient | None:
        query = select(OAuthClient).where(OAuthClient.client_id == client_id)
        if lock:
            # 所有签发与撤销按客户端加行锁，跨进程串行化同一客户端的凭据变更。
            query = query.with_for_update().execution_options(populate_existing=True)
        return self.session.scalar(query)

    def grant_valid(self, grant_id: str, user_id: str) -> bool:
        grant = self.session.get(OAuthGrant, grant_id)
        user = self.session.get(User, user_id)
        return bool(
            grant
            and grant.revoked_at is None
            and user
            and user.is_active
            and (user.sessions_valid_after is None or aware(grant.created_at) >= aware(user.sessions_valid_after))
        )

    def get_access_token(self, raw: str) -> OAuthToken | None:
        return self.session.scalar(select(OAuthToken).where(OAuthToken.access_token_hash == credential_hash(raw)))

    def get_refresh_token(self, raw: str) -> OAuthToken | None:
        return self.session.scalar(select(OAuthToken).where(OAuthToken.refresh_token_hash == credential_hash(raw)))

    def verify_access_token(self, raw: str, resource: str) -> OAuthToken | None:
        token = self.get_access_token(raw)
        if token is None or token.resource != resource or token.access_revoked_at or now() >= aware(token.expires_at):
            return None
        client = self.session.get(OAuthClient, token.client_record_id)
        if not client or client.disabled_at or not self.grant_valid(token.grant_id, token.user_id):
            return None
        return token

    def revoke_family(self, record, reason: str = "revoked") -> None:
        self.session.execute(
            update(OAuthToken)
            .where(OAuthToken.family_id == record.family_id)
            .values(access_revoked_at=now(), refresh_revoked_at=now(), revocation_reason=reason)
            .execution_options(synchronize_session="fetch")
        )

    def revoke_grant(self, grant: OAuthGrant) -> None:
        grant.revoked_at = now()
        self.session.execute(
            update(OAuthToken)
            .where(OAuthToken.grant_id == grant.id)
            .values(access_revoked_at=now(), refresh_revoked_at=now(), revocation_reason="grant_revoked")
        )

    def disable_client(self, client: OAuthClient) -> None:
        client.disabled_at = now()
        self.session.execute(
            update(OAuthGrant).where(OAuthGrant.client_record_id == client.id).values(revoked_at=now())
        )
        self.session.execute(
            update(OAuthToken)
            .where(OAuthToken.client_record_id == client.id)
            .values(access_revoked_at=now(), refresh_revoked_at=now(), revocation_reason="client_disabled")
        )

    def save_token(self, token: dict, request, settings) -> OAuthToken:
        old = request.refresh_token
        code = request.authorization_code
        timestamp = now()
        if old:
            claimed = self.session.execute(
                update(OAuthToken)
                .where(OAuthToken.id == old.id, OAuthToken.refresh_revoked_at.is_(None))
                .values(access_revoked_at=timestamp, refresh_revoked_at=timestamp, revocation_reason="rotated")
            ).rowcount
            parent = old
        else:
            claimed = self.session.execute(
                update(OAuthAuthorizationCode)
                .where(OAuthAuthorizationCode.id == code.id, OAuthAuthorizationCode.consumed_at.is_(None))
                .values(consumed_at=timestamp)
            ).rowcount
            parent = code
        if claimed != 1:
            self.revoke_family(parent, "replay")
            raise InvalidGrantError("Credential has already been consumed.")
        record = OAuthToken(
            id=new_id(),
            access_token_hash=credential_hash(token["access_token"]),
            refresh_token_hash=credential_hash(token["refresh_token"]) if token.get("refresh_token") else None,
            client_record_id=request.client.id,
            client_id=request.client.client_id,
            user_id=request.user,
            grant_id=parent.grant_id,
            family_id=parent.family_id,
            parent_token_id=old.id if old else None,
            scope=token.get("scope", ""),
            resource=request.payload.data["resource"],
            issued_at=timestamp,
            expires_at=timestamp + timedelta(seconds=token["expires_in"]),
            refresh_expires_at=timestamp + timedelta(seconds=settings.mcp_refresh_token_seconds)
            if token.get("refresh_token")
            else None,
        )
        self.session.add(record)
        request.client.last_used_at = timestamp
        self.session.flush()
        return record

    def record_replay(self, record) -> None:
        self.revoke_family(record, "replay")
        audit.record(
            self.session,
            self.session.get(User, record.user_id),
            "oauth.replay",
            "oauth_grant",
            record.grant_id,
            client_id=record.client_id,
            family_id=record.family_id,
        )
