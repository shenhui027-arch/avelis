"""Anonymous OAuth write budgets, lifecycle cleanup and compatible pagination."""

from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError
from sqlalchemy import event, func, select, text
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from avelis.oauth import limits
from avelis.oauth.cimd import CIMDError, validate_client_metadata
from avelis.oauth.cleanup import purge_batch
from avelis.oauth.repository import now
from avelis.oauth.router import create_oauth_router
from tests.unit.test_oauth_server import (
    BASE,
    REDIRECT,
    RESOURCE,
    authorization_params,
    exchange_params,
    issue_code,
    issue_ticket,
    issue_token,
    oauth_api,  # noqa: F401
    refresh,
)


def register(api, **changes):
    return api.client.post(
        "/oauth/register", json={"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none", **changes}
    )


def count(api, model):
    return api.session.scalar(select(func.count()).select_from(model))


def configure(api, **settings):
    # The router captures immutable settings, so replace it while retaining dependencies.
    api.settings = replace(api.settings, **settings)
    api.app.router.routes.clear()
    api.app.include_router(create_oauth_router(api.settings))


@pytest.mark.parametrize(
    "changes",
    [
        {"redirect_uris": [REDIRECT] * 6},
        {"redirect_uris": ["https://client.example/" + "x" * 512]},
        {"client_name": "x" * 257},
        {"client_uri": "https://client.example/" + "x" * 512},
        {"logo_uri": "https://client.example/" + "x" * 512},
        {"scope": "x" * 257},
        {"scope": " ".join(["a"] * 17)},
        {"grant_types": ["authorization_code"] * 6},
        {"response_types": ["code"] * 6},
        {"contacts": ["a@example.com"] * 6},
    ],
)
def test_registration_field_budgets_never_write(oauth_api, changes):
    api = oauth_api
    before = count(api, api.OAuthClient), count(api, api.AuditEvent)
    response = register(api, **changes)
    assert response.status_code == 400, response.text
    assert response.json()["error"] in ("invalid_client_metadata", "invalid_redirect_uri")
    assert (count(api, api.OAuthClient), count(api, api.AuditEvent)) == before


@pytest.mark.parametrize("chunked", [False, True])
def test_oversize_registration_body_is_bounded_without_content_length(oauth_api, chunked):
    api = oauth_api
    body = b'{"redirect_uris":["https://client.example/' + b"x" * 131109 + b'"]}'
    content = (part for part in (body[:100], body[100:])) if chunked else body
    response = api.client.post("/oauth/register", content=content, headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_client_metadata"
    assert count(api, api.OAuthClient) == 1 and count(api, api.AuditEvent) == 0
    assert register(api, client_name="SDK").status_code == 201


@pytest.mark.parametrize(
    "field,value",
    [
        ("redirect_uris", [REDIRECT] * 6),
        ("redirect_uris", ["https://example.com/" + "x" * 512]),
        ("client_name", "x" * 257),
        ("logo_uri", "https://example.com/" + "x" * 512),
        ("grant_types", ["authorization_code"] * 6),
        ("scope", "x" * 257),
    ],
)
def test_cimd_uses_the_same_field_budgets(field, value):
    url = "https://client.example/metadata.json"
    with pytest.raises(CIMDError, match="budget"):
        validate_client_metadata({"client_id": url, "redirect_uris": [REDIRECT], field: value}, url)


def test_registration_source_windows_are_independent_and_audit_only_once(oauth_api, monkeypatch):
    api = oauth_api
    configure(api, mcp_registration_max_per_hour=2)
    monkeypatch.setattr(limits.time, "time", lambda: 3601)
    assert register(api).status_code == 201
    assert register(api).status_code == 201
    for _ in range(3):
        response = register(api)
        assert response.status_code == 429 and response.headers["retry-after"] == "3599"
    with TestClient(api.app, base_url=BASE, client=("192.0.2.2", 123)) as other:
        assert other.post("/oauth/register", json={"redirect_uris": [REDIRECT]}).status_code == 201
    events = list(api.session.scalars(select(api.AuditEvent).where(api.AuditEvent.action == "oauth.budget_exceeded")))
    assert len(events) == 1
    monkeypatch.setattr(limits.time, "time", lambda: 7201)
    assert register(api).status_code == 201
    assert all(expiry == 3601 for expiry in api.counter.expiries.values())


def test_pending_quota_counts_only_never_authorized_dynamic_clients(oauth_api):
    api = oauth_api
    configure(api, mcp_max_pending_clients=1)
    for _ in range(3):
        response = register(api)
        assert response.status_code == 400 and response.json()["error"] == "invalid_client_metadata"
    assert count(api, api.OAuthClient) == 1
    assert count(api, api.AuditEvent) == 1  # one global rejection summary
    issue_code(api)
    grant = api.session.scalar(select(api.OAuthGrant))
    grant.revoked_at = now()
    api.session.commit()
    assert register(api).status_code == 201
    assert register(api).status_code == 400


@pytest.mark.parametrize(
    "field,length",
    [
        ("redirect_uri", 513),
        ("state", 513),
        ("scope", 257),
        ("resource", 2049),
        ("client_id", 2049),
        ("code_challenge", 129),
        ("extra", 513),
    ],
)
def test_authorization_parameter_budgets_precede_all_writes(oauth_api, field, length):
    api = oauth_api
    response = api.client.get("/oauth/authorize", params=authorization_params(**{field: "x" * length}))
    assert response.status_code == 400 and response.json()["error"] == "invalid_request"
    assert "location" not in response.headers
    assert count(api, api.OAuthConsentTicket) == count(api, api.AuditEvent) == 0


def test_consent_source_limit_and_registration_have_separate_counters(oauth_api):
    api = oauth_api
    configure(api, mcp_consent_max_per_hour=1)
    issue_ticket(api)
    for _ in range(3):
        assert api.client.get("/oauth/authorize", params=authorization_params()).status_code == 429
    assert count(api, api.OAuthConsentTicket) == 1
    assert count(api, api.AuditEvent) == 1
    assert register(api).status_code == 201
    with TestClient(api.app, base_url=BASE, client=("192.0.2.9", 123), follow_redirects=False) as other:
        assert other.get("/oauth/authorize", params=authorization_params()).status_code == 302


@pytest.mark.parametrize("trusted", [False, True])
def test_source_limit_obeys_uvicorn_proxy_trust(oauth_api, trusted):
    api = oauth_api
    configure(api, mcp_registration_max_per_hour=1)
    app = ProxyHeadersMiddleware(api.app, trusted_hosts="127.0.0.1")
    peer = "127.0.0.1" if trusted else "192.0.2.50"
    with TestClient(app, base_url=BASE, client=(peer, 123)) as client:
        for ip, expected in [("192.0.2.1", 201), ("192.0.2.2", 201 if trusted else 429)]:
            assert (
                client.post(
                    "/oauth/register",
                    json={"redirect_uris": [REDIRECT]},
                    headers={"X-Forwarded-For": ip, "X-Forwarded-Host": "spoof.example"},
                ).status_code
                == expected
            )


@pytest.mark.parametrize("path", ["/oauth/register", "/oauth/authorize"])
def test_redis_failure_refuses_anonymous_writes(oauth_api, monkeypatch, path):
    def unavailable(*_):
        raise ConnectionError("offline")

    monkeypatch.setattr("avelis.cache.client", unavailable)
    api = oauth_api
    response = register(api) if path.endswith("register") else api.client.get(path, params=authorization_params())
    assert response.status_code == 503
    assert count(api, api.OAuthClient) == 1
    assert count(api, api.OAuthConsentTicket) == count(api, api.AuditEvent) == 0


def test_cleanup_preserves_replay_window_live_refresh_and_authorized_clients(oauth_api):
    api = oauth_api
    api.session.execute(text("PRAGMA foreign_keys=ON"))
    token = issue_token(api)
    rotated = refresh(api, token).json()
    old = api.repository.get_refresh_token(token["refresh_token"])
    live = api.repository.get_refresh_token(rotated["refresh_token"])
    timestamp = now()
    old.expires_at = old.refresh_expires_at = timestamp - timedelta(days=6)
    old.access_revoked_at = old.refresh_revoked_at = timestamp - timedelta(days=6)
    live.expires_at = timestamp - timedelta(days=10)
    issue_code(api)
    for ticket in api.session.scalars(select(api.OAuthConsentTicket)):
        ticket.expires_at = timestamp - timedelta(seconds=1)
    for code in api.session.scalars(select(api.OAuthAuthorizationCode)):
        code.expires_at = timestamp - timedelta(seconds=1)
    client = api.session.scalar(select(api.OAuthClient))
    client.created_at = timestamp - timedelta(days=10)
    grant = api.session.scalar(select(api.OAuthGrant))
    grant.revoked_at = timestamp  # historical authorization still protects the client
    pending = api.OAuthClient(
        client_id="pending",
        source="dynamic",
        redirect_uris=[REDIRECT],
        scope="metrics:read",
        grant_types=["authorization_code"],
        response_types=["code"],
        created_at=timestamp - timedelta(days=10),
    )
    api.session.add(pending)
    api.session.commit()
    summary = purge_batch(api.session, api.settings, timestamp)
    api.session.commit()
    # 只删未用过的那个授权码；已换过令牌的授权码是重放检测的依据，令牌族还在时保留
    assert summary["oauth_consent_ticket"] == 2 and summary["oauth_authorization_code"] == 1
    assert summary["oauth_client"] == 1 and summary["oauth_token"] == 0
    assert api.session.get(api.OAuthClient, client.id) is not None
    old.expires_at = old.refresh_expires_at = timestamp - timedelta(days=8)
    old.access_revoked_at = old.refresh_revoked_at = timestamp - timedelta(days=8)
    api.session.commit()
    summary = purge_batch(api.session, api.settings, timestamp)
    api.session.commit()
    assert summary["oauth_token"] == 1
    assert api.repository.get_refresh_token(token["refresh_token"]) is None
    api.session.refresh(live)
    assert live.parent_token_id is None  # child remains valid despite deleting its parent


def test_cleanup_batches_are_bounded_and_do_not_audit(oauth_api):
    api = oauth_api
    for _ in range(3):
        issue_ticket(api)
    timestamp = now()
    for ticket in api.session.scalars(select(api.OAuthConsentTicket)):
        ticket.expires_at = timestamp - timedelta(seconds=1)
    api.session.commit()
    before = count(api, api.AuditEvent)
    settings = replace(api.settings, mcp_cleanup_batch_size=2)
    assert purge_batch(api.session, settings, timestamp)["oauth_consent_ticket"] == 2
    assert count(api, api.OAuthConsentTicket) == 1
    assert purge_batch(api.session, settings, timestamp)["oauth_consent_ticket"] == 1
    assert count(api, api.AuditEvent) == before


def test_cleanup_locks_clients_before_any_credential_mutation(oauth_api):
    api = oauth_api
    issue_code(api)
    timestamp = now()
    for model in (api.OAuthConsentTicket, api.OAuthAuthorizationCode):
        for row in api.session.scalars(select(model)):
            row.expires_at = timestamp - timedelta(seconds=1)
    api.session.commit()
    statements = []

    def observe(execution):
        statement = execution.statement
        if getattr(statement, "_for_update_arg", None) is not None:
            statements.append("lock")
        elif execution.is_delete or execution.is_update:
            statements.append("mutate")

    event.listen(api.session, "do_orm_execute", observe)
    try:
        purge_batch(api.session, api.settings, timestamp)
    finally:
        event.remove(api.session, "do_orm_execute", observe)
    assert statements[0] == "lock" and "mutate" in statements


def test_cleanup_drains_live_tickets_of_stale_pending_clients_in_bounded_batches(oauth_api):
    api = oauth_api
    for _ in range(3):
        issue_ticket(api)
    timestamp = now()
    client = api.session.scalar(select(api.OAuthClient))
    client.created_at = timestamp - timedelta(days=10)
    api.session.commit()
    settings = replace(api.settings, mcp_cleanup_batch_size=2)
    first = purge_batch(api.session, settings, timestamp)
    assert first["oauth_client"] == 0 and first["oauth_consent_ticket"] == 2
    second = purge_batch(api.session, settings, timestamp)
    assert second["oauth_client"] == 1 and second["oauth_consent_ticket"] == 1


def test_quota_uses_postgresql_transaction_lock_before_count():
    from types import SimpleNamespace

    calls = []
    session = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
        execute=lambda statement: calls.append(str(statement)),
        scalar=lambda statement: calls.append(str(statement)) or 0,
    )
    assert limits.pending_quota_available(session, 1000)
    assert "pg_advisory_xact_lock" in calls[0]
    assert "count" in calls[1] and "EXISTS" in calls[1]


def test_redis_failure_while_reporting_global_quota_also_refuses_writes(oauth_api, monkeypatch):
    api = oauth_api
    configure(api, mcp_max_pending_clients=1)
    original = limits.window_count

    def fail_summary(action, source, seconds=3600):
        if action == "pending_quota":
            raise ConnectionError("offline")
        return original(action, source, seconds)

    monkeypatch.setattr(limits, "window_count", fail_summary)
    response = register(api)
    assert response.status_code == 503
    assert count(api, api.OAuthClient) == 1 and count(api, api.AuditEvent) == 0


def test_client_list_is_a_compatible_bounded_page(oauth_api):
    api = oauth_api
    api.actor = api.admin
    configure(api, mcp_registration_max_per_hour=100)
    for _ in range(54):
        assert register(api).status_code == 201
    first = api.client.get("/api/oauth/clients").json()
    second = api.client.get("/api/oauth/clients", params={"limit": 50, "offset": 50}).json()
    assert len(first) == 50 and len(second) == 5
    assert not {c["id"] for c in first} & {c["id"] for c in second}
    assert "authorization_count" in first[0]
    for params in ({"limit": 201}, {"limit": 0}, {"offset": -1}):
        assert api.client.get("/api/oauth/clients", params=params).status_code == 422


def test_cleanup_keeps_consumed_codes_as_replay_tripwire_until_family_is_gone(oauth_api):
    api = oauth_api
    code = issue_code(api)
    token = api.client.post("/oauth/token", data=exchange_params(code)).json()
    timestamp = now()
    row = api.session.scalar(select(api.OAuthAuthorizationCode))
    row.expires_at = timestamp - timedelta(days=30)
    row.consumed_at = timestamp - timedelta(days=30)
    api.session.commit()
    # 授权码早已过期且超过重放窗口，但它换出的令牌族还在：必须保留，重放仍能撤销整族
    assert purge_batch(api.session, api.settings, timestamp)["oauth_authorization_code"] == 0
    api.session.commit()
    replay = api.client.post("/oauth/token", data=exchange_params(code))
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    assert api.repository.verify_access_token(token["access_token"], RESOURCE) is None
    # 令牌族全部清理之后，授权码才随之删除
    for t in api.session.scalars(select(api.OAuthToken)):
        t.expires_at = t.refresh_expires_at = timestamp - timedelta(days=30)
        t.access_revoked_at = t.refresh_revoked_at = timestamp - timedelta(days=30)
    api.session.commit()
    assert purge_batch(api.session, api.settings, timestamp)["oauth_token"] >= 1
    api.session.commit()
    assert purge_batch(api.session, api.settings, timestamp)["oauth_authorization_code"] == 1
