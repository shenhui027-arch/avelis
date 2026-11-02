"""登录续期 HTTP 合约：真实 JWT、认证依赖和缓存逻辑，使用隔离 SQLite 与内存 Redis。"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from math import ceil
from types import SimpleNamespace

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import JSON, MetaData, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def login(api, password="Initial-pass-2026"):
    return api.client.post("/api/auth/token", data={"username": api.user.username, "password": password})


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("AVELIS_SECRET", "offline-unit-test-secret-0123456789")
    monkeypatch.setenv("AVELIS_ENV_FILE", "/nonexistent")
    from avelis import cache, security
    from avelis.api import auth
    from avelis.api.deps import get_session
    from avelis.config import build_settings
    from avelis.models import AuditEvent, Domain, DomainMember, EventOutbox, User, UserRole, WebhookSubscription

    class Clock(datetime):
        moment = datetime.now(timezone.utc).replace(microsecond=0)

        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(cls.moment.timestamp(), tz)

    class MemoryRedis:
        def __init__(self):
            self.values, self.deadlines, self.writes = {}, {}, []

        def get(self, name):
            if self.deadlines.get(name, float("inf")) <= Clock.now(timezone.utc).timestamp():
                self.delete(name)
            return self.values.get(name)

        def set(self, name, value, ex):
            self.values[name] = value
            self.expire(name, ex)
            self.writes.append((name, ex))

        def exists(self, name):
            return int(self.get(name) is not None)

        def incr(self, name):
            self.values[name] = int(self.get(name) or 0) + 1
            return self.values[name]

        def expire(self, name, seconds):
            self.deadlines[name] = Clock.now(timezone.utc).timestamp() + seconds

        def ttl(self, name):
            return ceil(self.deadlines[name] - Clock.now(timezone.utc).timestamp())

        def delete(self, name):
            self.values.pop(name, None)
            self.deadlines.pop(name, None)

    state = SimpleNamespace(
        settings=build_settings({"AVELIS_SECRET": "offline-unit-test-secret-0123456789"}),
        clock=Clock,
        store=MemoryRedis(),
        security=security,
        cache=cache,
        AuditEvent=AuditEvent,
    )
    monkeypatch.setattr(security, "get_settings", lambda: state.settings)
    monkeypatch.setattr(auth, "get_settings", lambda: state.settings)
    monkeypatch.setattr(security, "datetime", Clock)
    monkeypatch.setattr(auth, "datetime", Clock)
    monkeypatch.setattr(jwt.api_jwt, "datetime", Clock)
    monkeypatch.setattr(cache, "client", lambda: state.store)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in (User, UserRole, Domain, DomainMember, AuditEvent, WebhookSubscription, EventOutbox):
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(engine)
    app = FastAPI()
    app.include_router(auth.router)
    try:
        with Session(engine, expire_on_commit=False) as session:
            state.session = session
            state.user = User(
                id="actor",
                username="actor",
                display_name="登录用户",
                password_hash=security.hash_password("Initial-pass-2026"),
                must_change_password=True,
            )
            state.admin = User(id="admin", username="admin", display_name="管理员", password_hash="unused")
            session.add_all([state.user, state.admin, UserRole(user_id="admin", role="admin")])
            session.commit()

            def session_override():
                try:
                    yield session
                    session.commit()
                except Exception:
                    session.rollback()
                    raise

            app.dependency_overrides[get_session] = session_override
            with TestClient(app) as client:
                state.client = client
                yield state
    finally:
        engine.dispose()


@pytest.mark.parametrize("must_change_password", [False, True])
def test_login_and_chained_refresh_rotate_tokens_and_preserve_session(api, must_change_password):
    api.user.must_change_password = must_change_password
    api.session.commit()
    response = login(api)
    assert response.status_code == 200, response.text
    data = response.json()
    original = api.security.decode_access_token(data["access_token"])
    assert original["auth_time"] == int(api.clock.moment.timestamp())
    assert data["expires_in"] == 120 * 60
    assert datetime.fromisoformat(data["expires_at"]).timestamp() == original["exp"]
    assert data["token_type"] == "bearer" and data["must_change_password"] is must_change_password
    for minutes in (10, 15):
        old = data["access_token"]
        old_claims = api.security.decode_access_token(old)
        api.clock.moment += timedelta(minutes=minutes)
        response = api.client.post("/api/auth/refresh", headers=bearer(old))
        assert response.status_code == 200, response.text
        data = response.json()
        claims = api.security.decode_access_token(data["access_token"])
        assert claims["jti"] != old_claims["jti"]
        assert claims["auth_time"] == original["auth_time"]
        assert claims["exp"] == int(api.clock.moment.timestamp()) + 120 * 60
        assert data["expires_in"] == 120 * 60
        assert datetime.fromisoformat(data["expires_at"]).timestamp() == claims["exp"]
        assert data["token_type"] == "bearer" and data["must_change_password"] is must_change_password
        assert api.store.writes[-1] == (
            f"avelis:revoked:{old_claims['jti']}",
            old_claims["exp"] - int(api.clock.moment.timestamp()),
        )
        assert api.client.get("/api/me", headers=bearer(old)).status_code == 401
        assert api.client.post("/api/auth/refresh", headers=bearer(old)).status_code == 401
        me = api.client.get("/api/me", headers=bearer(data["access_token"]))
        assert me.status_code == 200 and me.json()["must_change_password"] is must_change_password
    events = list(api.session.scalars(select(api.AuditEvent)))
    assert [event.action for event in events] == ["auth.login", "auth.refresh", "auth.refresh"]
    assert all(event.actor_id == api.user.id and "token" not in repr(event.detail) for event in events)


def test_login_also_caps_expiry_at_session_maximum(api):
    api.settings = replace(api.settings, access_token_minutes=180, session_max_hours=1)
    response = login(api)
    assert response.status_code == 200, response.text
    data = response.json()
    claims = api.security.decode_access_token(data["access_token"])
    assert claims["exp"] == claims["auth_time"] + 3600
    assert data["expires_in"] == 3600
    assert datetime.fromisoformat(data["expires_at"]).timestamp() == claims["exp"]


def test_refresh_caps_expiry_at_original_session_maximum(api):
    auth_time = int(api.clock.moment.timestamp()) - 167 * 3600
    token, _, _ = api.security.create_access_token(api.user.id, api.user.username, auth_time=auth_time)
    api.clock.moment += timedelta(minutes=15)
    response = api.client.post("/api/auth/refresh", headers=bearer(token))
    assert response.status_code == 200, response.text
    data = response.json()
    claims = api.security.decode_access_token(data["access_token"])
    assert claims["auth_time"] == auth_time
    assert claims["exp"] == auth_time + 168 * 3600
    assert data["expires_in"] == 45 * 60
    assert datetime.fromisoformat(data["expires_at"]).timestamp() == claims["exp"]


@pytest.mark.parametrize("overrun_seconds", [0, 1])
def test_refresh_rejects_session_at_or_beyond_maximum(api, overrun_seconds):
    # 模拟调整最长时长后，当前 JWT 尚有效、登录时长已达到新上限。
    api.settings = replace(api.settings, session_max_hours=169)
    auth_time = int(api.clock.moment.timestamp()) - 168 * 3600 - overrun_seconds
    token, _, _ = api.security.create_access_token(api.user.id, api.user.username, auth_time=auth_time)
    api.settings = replace(api.settings, session_max_hours=168)
    response = api.client.post("/api/auth/refresh", headers=bearer(token))
    assert response.status_code == 401
    assert response.json()["detail"] == "登录已超过最长时长，请重新登录"
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("state", ["missing", "expired", "disabled", "logged_out"])
def test_refresh_requires_current_active_non_revoked_token(api, state):
    token = login(api).json()["access_token"]
    headers = bearer(token)
    if state == "missing":
        headers = {}
    elif state == "expired":
        api.clock.moment += timedelta(minutes=120, seconds=1)
    elif state == "disabled":
        api.user.is_active = False
        api.session.commit()
    else:
        assert api.client.post("/api/auth/logout", headers=headers).status_code == 200
    response = api.client.post("/api/auth/refresh", headers=headers)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("action", ["change", "reset"])
def test_password_change_or_admin_reset_prevents_old_token_refresh(api, action):
    old = login(api).json()["access_token"]
    old_claims = api.security.decode_access_token(old)
    api.clock.moment += timedelta(microseconds=500_000)
    if action == "change":
        response = api.client.post(
            "/api/me/password",
            headers=bearer(old),
            json={"current_password": "Initial-pass-2026", "new_password": "Brand-new-2026"},
        )
        assert response.status_code == 200, response.text
        fresh = response.json()["access_token"]
        assert api.security.decode_access_token(fresh)["auth_time"] == old_claims["auth_time"]
    else:
        admin_token, _, _ = api.security.create_access_token(api.admin.id, api.admin.username)
        response = api.client.post(
            f"/api/users/{api.user.id}/password", headers=bearer(admin_token), json={"new_password": "Brand-new-2026"}
        )
        assert response.status_code == 200, response.text
        fresh = login(api, password="Brand-new-2026").json()["access_token"]
    rejected = api.client.post("/api/auth/refresh", headers=bearer(old))
    assert rejected.status_code == 401 and "会话已失效" in rejected.json()["detail"]
    refreshed = api.client.post("/api/auth/refresh", headers=bearer(fresh))
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["must_change_password"] is (action == "reset")


def test_password_change_preserves_original_session_maximum(api):
    auth_time = int(api.clock.moment.timestamp()) - int(timedelta(hours=167, minutes=30).total_seconds())
    old, _, _ = api.security.create_access_token(api.user.id, api.user.username, auth_time=auth_time)
    api.clock.moment += timedelta(microseconds=500_000)
    response = api.client.post(
        "/api/me/password",
        headers=bearer(old),
        json={"current_password": "Initial-pass-2026", "new_password": "Brand-new-2026"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    claims = api.security.decode_access_token(data["access_token"])
    assert claims["auth_time"] == auth_time
    assert claims["exp"] == auth_time + 168 * 3600
    assert datetime.fromisoformat(data["expires_at"]).timestamp() == claims["exp"]
    assert api.client.post("/api/auth/refresh", headers=bearer(old)).status_code == 401
    assert api.client.get("/api/me", headers=bearer(data["access_token"])).status_code == 200


def test_login_lock_does_not_limit_refresh(api):
    token = login(api).json()["access_token"]
    for _ in range(api.settings.login_max_failures):
        assert login(api, password="wrong-password").status_code == 401
    assert login(api).status_code == 429
    response = api.client.post("/api/auth/refresh", headers=bearer(token))
    assert response.status_code == 200, response.text
    assert login(api).status_code == 429  # 续期也不清除登录失败记录。


def test_legacy_token_without_auth_time_uses_original_iat(api):
    claims = api.security.decode_access_token(login(api).json()["access_token"])
    del claims["auth_time"]
    token = jwt.encode(claims, api.security._jwt_key(api.settings.secret), algorithm=api.security.JWT_ALGORITHM)
    api.clock.moment += timedelta(minutes=10)
    response = api.client.post("/api/auth/refresh", headers=bearer(token))
    assert response.status_code == 200, response.text
    refreshed = api.security.decode_access_token(response.json()["access_token"])
    assert refreshed["auth_time"] == claims["iat"]
    assert refreshed["iat"] > refreshed["auth_time"]
    assert api.client.post("/api/auth/refresh", headers=bearer(token)).status_code == 401
