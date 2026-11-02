"""OAuth 协议与管理接口：真实 Authlib、确认票据和隔离 SQLite 存储。"""

from __future__ import annotations

from datetime import timedelta
from hashlib import sha256
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import JSON, MetaData, create_engine, inspect, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

BASE = "https://avelis.example"
RESOURCE = BASE + "/mcp"
REDIRECT = "http://127.0.0.1:9876/callback"
VERIFIER = "a" * 64
SCOPES = "metrics:read lineage:read ask:execute"


@pytest.fixture
def oauth_api():
    from avelis.api.deps import current_user, get_session
    from avelis.governance.errors import GovernanceError
    from avelis.models import (
        AuditEvent,
        Domain,
        DomainMember,
        OAuthAuthorizationCode,
        OAuthClient,
        OAuthConsentTicket,
        OAuthGrant,
        OAuthToken,
        User,
        UserRole,
    )
    from avelis.oauth.repository import Repository
    from avelis.oauth.router import create_oauth_router

    settings = SimpleNamespace(
        public_url=BASE,
        mcp_enabled=True,
        mcp_access_token_seconds=900,
        mcp_refresh_token_seconds=30 * 24 * 3600,
        mcp_cimd_cache_ttl_seconds=3600,
        mcp_cimd_allowed_urls=(),
    )
    models = (
        User,
        UserRole,
        Domain,
        DomainMember,
        AuditEvent,
        OAuthClient,
        OAuthGrant,
        OAuthConsentTicket,
        OAuthAuthorizationCode,
        OAuthToken,
    )
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in models:
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(engine)
    app = FastAPI()

    @app.exception_handler(GovernanceError)
    async def governance_error(_: Request, error: GovernanceError):
        return JSONResponse({"detail": str(error.message)}, status_code=error.status_code)

    app.include_router(create_oauth_router(settings))
    try:
        with Session(engine, expire_on_commit=False) as session:
            user = User(id="oauth-user", username="actor", display_name="Actor", password_hash="unused")
            other = User(id="other-user", username="other", display_name="Other", password_hash="unused")
            admin = User(id="admin-user", username="admin", display_name="Admin", password_hash="unused")
            session.add_all(
                [
                    user,
                    other,
                    admin,
                    UserRole(user_id=user.id, role="analyst"),
                    UserRole(user_id=admin.id, role="admin"),
                    Domain(id="visible", code="visible", name="Visible", owner_id=admin.id),
                    Domain(id="hidden", code="hidden", name="Hidden", owner_id=admin.id),
                    DomainMember(user_id=user.id, domain_id="visible", role="viewer"),
                    OAuthClient(
                        client_id="test-client",
                        client_name="Test",
                        redirect_uris=[REDIRECT],
                        scope=SCOPES,
                        grant_types=["authorization_code", "refresh_token"],
                        response_types=["code"],
                        token_endpoint_auth_method="none",
                        source="dynamic",
                    ),
                ]
            )
            session.commit()
            state = SimpleNamespace(
                session=session,
                settings=settings,
                user=user,
                other=other,
                admin=admin,
                actor=user,
                app=app,
                repository=Repository(session),
                AuditEvent=AuditEvent,
                OAuthClient=OAuthClient,
                OAuthGrant=OAuthGrant,
                OAuthConsentTicket=OAuthConsentTicket,
                OAuthAuthorizationCode=OAuthAuthorizationCode,
                OAuthToken=OAuthToken,
                models=models,
            )

            def session_override():
                try:
                    yield session
                    session.commit()
                except Exception:
                    session.rollback()
                    raise

            app.dependency_overrides[get_session] = session_override
            app.dependency_overrides[current_user] = lambda: state.actor
            with TestClient(app, base_url=BASE, follow_redirects=False) as client:
                state.client = client
                yield state
    finally:
        engine.dispose()


def authorization_params(**changes):
    params = {
        "response_type": "code",
        "client_id": "test-client",
        "redirect_uri": REDIRECT,
        "scope": "metrics:read",
        "state": "test-state",
        "resource": RESOURCE,
        "code_challenge": create_s256_code_challenge(VERIFIER),
        "code_challenge_method": "S256",
    }
    params.update(changes)
    return {key: value for key, value in params.items() if value is not None}


def callback_params(response):
    assert response.status_code == 302, response.text
    return parse_qs(urlsplit(response.headers["location"]).query)


def issue_ticket(api, **changes):
    response = api.client.get("/oauth/authorize", params=authorization_params(**changes))
    assert response.status_code == 302, response.text
    target = urlsplit(response.headers["location"])
    assert target.path == "/oauth/consent"
    assert "code" not in parse_qs(target.query)
    return parse_qs(target.query)["ticket"][0]


def issue_code(api, **changes):
    ticket = issue_ticket(api, **changes)
    approved = api.client.post(f"/api/oauth/consent/{ticket}/approve")
    assert approved.status_code == 200, approved.text
    callback = urlsplit(approved.json()["redirect_to"])
    assert callback.scheme + "://" + callback.netloc + callback.path == REDIRECT
    params = parse_qs(callback.query)
    assert params["iss"] == [BASE]
    assert params["state"] == ["test-state"]
    return params["code"][0]


def exchange_params(code, **changes):
    data = {
        "grant_type": "authorization_code",
        "client_id": "test-client",
        "redirect_uri": REDIRECT,
        "code": code,
        "code_verifier": VERIFIER,
        "resource": RESOURCE,
    }
    data.update(changes)
    return {key: value for key, value in data.items() if value is not None}


def issue_token(api, **changes):
    response = api.client.post("/oauth/token", data=exchange_params(issue_code(api, **changes)))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def refresh(api, token, **changes):
    data = {
        "grant_type": "refresh_token",
        "client_id": "test-client",
        "refresh_token": token["refresh_token"],
        "resource": RESOURCE,
    }
    data.update(changes)
    return api.client.post("/oauth/token", data={key: value for key, value in data.items() if value is not None})


def stored_rows(api, *models):
    return [
        {column.key: getattr(record, column.key) for column in inspect(model).columns}
        for model in models
        for record in api.session.scalars(select(model))
    ]


def test_metadata_and_full_authlib_lifecycle(oauth_api):
    api = oauth_api
    metadata = api.client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["issuer"] == BASE
    assert metadata["authorization_endpoint"] == BASE + "/oauth/authorize"
    assert metadata["token_endpoint"] == BASE + "/oauth/token"
    assert metadata["registration_endpoint"] == BASE + "/oauth/register"
    assert metadata["revocation_endpoint"] == BASE + "/oauth/revoke"
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["authorization_response_iss_parameter_supported"] is True
    token = issue_token(api)
    assert token["resource"] == RESOURCE
    assert token["expires_in"] == 900
    old = api.repository.verify_access_token(token["access_token"], RESOURCE)
    assert old is not None and old.user_id == api.user.id
    response = refresh(api, token)
    assert response.status_code == 200, response.text
    refreshed = response.json()
    assert refreshed["access_token"] != token["access_token"]
    assert refreshed["refresh_token"] != token["refresh_token"]
    assert refreshed["resource"] == RESOURCE
    assert api.repository.verify_access_token(token["access_token"], RESOURCE) is None
    new = api.repository.verify_access_token(refreshed["access_token"], RESOURCE)
    assert new is not None and new.family_id == old.family_id
    assert new.scope == old.scope
    response = api.client.post(
        "/oauth/revoke",
        data={
            "token": refreshed["refresh_token"],
            "client_id": "test-client",
            "token_type_hint": "refresh_token",
        },
    )
    assert response.status_code == 200, response.text
    assert api.repository.verify_access_token(refreshed["access_token"], RESOURCE) is None
    assert refresh(api, refreshed).json()["error"] == "invalid_grant"


@pytest.mark.parametrize(
    "changes",
    [
        {"code_challenge_method": "plain"},
        {"code_challenge_method": None},
        {"code_challenge": None},
        {"code_challenge": None, "code_challenge_method": None},
    ],
)
def test_only_explicit_s256(oauth_api, changes):
    response = oauth_api.client.get("/oauth/authorize", params=authorization_params(**changes))
    params = callback_params(response)
    assert params["error"] == ["invalid_request"]
    assert params["iss"] == [BASE]
    assert not stored_rows(oauth_api, oauth_api.OAuthAuthorizationCode, oauth_api.OAuthConsentTicket)


@pytest.mark.parametrize("resource", [None, BASE, RESOURCE + "/", "https://other.example/mcp"])
def test_authorization_resource_required_and_exact(oauth_api, resource):
    response = oauth_api.client.get("/oauth/authorize", params=authorization_params(resource=resource))
    assert callback_params(response)["error"] == ["invalid_target"]
    assert not stored_rows(oauth_api, oauth_api.OAuthAuthorizationCode, oauth_api.OAuthConsentTicket)


@pytest.mark.parametrize("resource", [None, RESOURCE + "/", "https://other.example/mcp"])
def test_code_exchange_resource_cannot_change_or_be_omitted(oauth_api, resource):
    code = issue_code(oauth_api)
    response = oauth_api.client.post("/oauth/token", data=exchange_params(code, resource=resource))
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_target"
    assert not stored_rows(oauth_api, oauth_api.OAuthToken)
    assert oauth_api.client.post("/oauth/token", data=exchange_params(code)).status_code == 200


@pytest.mark.parametrize("resource", [None, RESOURCE + "/", "https://other.example/mcp"])
def test_refresh_resource_cannot_change_or_be_omitted(oauth_api, resource):
    token = issue_token(oauth_api)
    response = refresh(oauth_api, token, resource=resource)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_target"
    assert oauth_api.repository.verify_access_token(token["access_token"], RESOURCE) is not None
    assert refresh(oauth_api, token).status_code == 200


def test_pkce_wrong_verifier_does_not_consume_code(oauth_api):
    code = issue_code(oauth_api)
    wrong = oauth_api.client.post("/oauth/token", data=exchange_params(code, code_verifier="b" * 64))
    assert wrong.json()["error"] == "invalid_grant"
    assert oauth_api.client.post("/oauth/token", data=exchange_params(code)).status_code == 200


def test_authorization_code_replay_revokes_entire_token_family(oauth_api):
    code = issue_code(oauth_api)
    token = oauth_api.client.post("/oauth/token", data=exchange_params(code)).json()
    refreshed = refresh(oauth_api, token).json()
    assert oauth_api.repository.verify_access_token(refreshed["access_token"], RESOURCE) is not None
    replay = oauth_api.client.post("/oauth/token", data=exchange_params(code))
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    assert oauth_api.repository.verify_access_token(refreshed["access_token"], RESOURCE) is None
    assert refresh(oauth_api, refreshed).json()["error"] == "invalid_grant"


def test_refresh_token_replay_revokes_entire_token_family(oauth_api):
    token = issue_token(oauth_api)
    refreshed = refresh(oauth_api, token).json()
    assert oauth_api.repository.verify_access_token(refreshed["access_token"], RESOURCE) is not None
    replay = refresh(oauth_api, token)
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    assert oauth_api.repository.verify_access_token(refreshed["access_token"], RESOURCE) is None
    assert refresh(oauth_api, refreshed).json()["error"] == "invalid_grant"


def test_replay_revocation_does_not_affect_another_grant_family(oauth_api):
    token = issue_token(oauth_api)
    unrelated = issue_token(oauth_api)
    refreshed = refresh(oauth_api, token).json()
    assert refresh(oauth_api, token).json()["error"] == "invalid_grant"
    assert oauth_api.repository.verify_access_token(refreshed["access_token"], RESOURCE) is None
    assert oauth_api.repository.verify_access_token(unrelated["access_token"], RESOURCE) is not None


def test_code_expiry_and_redirect_binding(oauth_api):
    from avelis.db import utcnow

    code = issue_code(oauth_api)
    wrong = oauth_api.client.post("/oauth/token", data=exchange_params(code, redirect_uri=REDIRECT + "2"))
    assert wrong.json()["error"] == "invalid_grant"
    record = oauth_api.session.scalar(select(oauth_api.OAuthAuthorizationCode))
    record.expires_at = utcnow() - timedelta(seconds=1)
    oauth_api.session.commit()
    expired = oauth_api.client.post("/oauth/token", data=exchange_params(code))
    assert expired.json()["error"] == "invalid_grant"


def test_credentials_and_confirmation_tickets_only_retained_as_hashes(oauth_api):
    ticket = issue_ticket(oauth_api)
    approved = oauth_api.client.post(f"/api/oauth/consent/{ticket}/approve")
    code = parse_qs(urlsplit(approved.json()["redirect_to"]).query)["code"][0]
    token = oauth_api.client.post("/oauth/token", data=exchange_params(code)).json()
    persisted = repr(
        stored_rows(
            oauth_api,
            oauth_api.OAuthConsentTicket,
            oauth_api.OAuthAuthorizationCode,
            oauth_api.OAuthToken,
        )
    )
    for secret in (ticket, code, token["access_token"], token["refresh_token"]):
        assert secret not in persisted
        assert sha256(secret.encode()).hexdigest() in persisted


def test_dynamic_registration_validates_metadata_and_hashes_client_secret(oauth_api):
    response = oauth_api.client.post(
        "/oauth/register",
        json={
            "client_name": "Registered",
            "redirect_uris": [REDIRECT],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
            "scope": "metrics:read",
        },
    )
    assert response.status_code == 201, response.text
    document = response.json()
    records = stored_rows(oauth_api, oauth_api.OAuthClient)
    record = next(row for row in records if row["client_id"] == document["client_id"])
    assert record["source"] == "dynamic"
    assert document["client_secret"] not in repr(records)
    assert sha256(document["client_secret"].encode()).hexdigest() in repr(record)
    bad = oauth_api.client.post("/oauth/register", json={"redirect_uris": [REDIRECT], "grant_types": ["password"]})
    assert bad.status_code == 400
    assert bad.json()["error"] == "invalid_client_metadata"


def test_registered_confidential_client_authenticates_against_hash(oauth_api):
    registered = oauth_api.client.post(
        "/oauth/register",
        json={
            "redirect_uris": [REDIRECT],
            "grant_types": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_method": "client_secret_post",
            "scope": "metrics:read",
        },
    )
    info = registered.json()
    code = issue_code(oauth_api, client_id=info["client_id"])
    data = exchange_params(code, client_id=info["client_id"], client_secret="wrong")
    wrong = oauth_api.client.post("/oauth/token", data=data)
    assert wrong.status_code == 401
    assert wrong.json()["error"] == "invalid_client"
    data["client_secret"] = info["client_secret"]
    correct = oauth_api.client.post("/oauth/token", data=data)
    assert correct.status_code == 200, correct.text


def test_refresh_cannot_elevate_scope_and_revocation_checks_client_owner(oauth_api):
    api = oauth_api
    token = issue_token(api)
    elevated = refresh(api, token, scope="metrics:read ask:execute")
    assert elevated.json()["error"] == "invalid_scope"
    api.session.add(
        api.OAuthClient(
            client_id="other-client",
            client_name="Other",
            redirect_uris=[REDIRECT],
            scope=SCOPES,
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
            source="dynamic",
        )
    )
    api.session.commit()
    response = api.client.post("/oauth/revoke", data={"client_id": "other-client", "token": token["access_token"]})
    assert response.json()["error"] == "invalid_grant"
    assert api.repository.verify_access_token(token["access_token"], RESOURCE) is not None
    unknown = api.client.post("/oauth/revoke", data={"client_id": "test-client", "token": "unknown"})
    assert unknown.status_code == 200


def test_refresh_can_narrow_scope_and_preserves_narrowed_scope(oauth_api):
    token = issue_token(oauth_api, scope="metrics:read lineage:read")
    response = refresh(oauth_api, token, scope="metrics:read")
    assert response.status_code == 200, response.text
    assert response.json()["scope"] == "metrics:read"
    next_response = refresh(oauth_api, response.json())
    assert next_response.status_code == 200, next_response.text
    assert next_response.json()["scope"] == "metrics:read"


@pytest.mark.parametrize("credential", ["access", "refresh"])
def test_expired_credentials_are_rejected(oauth_api, credential):
    from avelis.db import utcnow

    token = issue_token(oauth_api)
    stored = oauth_api.session.scalar(select(oauth_api.OAuthToken))
    setattr(stored, "expires_at" if credential == "access" else "refresh_expires_at", utcnow() - timedelta(seconds=1))
    oauth_api.session.commit()
    if credential == "access":
        assert oauth_api.repository.verify_access_token(token["access_token"], RESOURCE) is None
    else:
        assert refresh(oauth_api, token).json()["error"] == "invalid_grant"


@pytest.mark.parametrize("change", ["disabled", "sessions_cutoff"])
def test_oauth_tokens_follow_current_user_account_state(oauth_api, change):
    from avelis.db import utcnow

    token = issue_token(oauth_api)
    if change == "disabled":
        oauth_api.user.is_active = False
    else:
        oauth_api.user.sessions_valid_after = utcnow() + timedelta(seconds=1)
    oauth_api.session.commit()
    assert oauth_api.repository.verify_access_token(token["access_token"], RESOURCE) is None
    assert refresh(oauth_api, token).json()["error"] == "invalid_grant"


@pytest.mark.parametrize(
    "changes",
    [
        {"redirect_uris": [1]},
        {"redirect_uris": REDIRECT},
        {"scope": ["metrics:read"]},
        {"grant_types": "authorization_code"},
        {"response_types": [None]},
    ],
)
def test_dynamic_registration_malformed_json_types_return_oauth_error(oauth_api, changes):
    data = {"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none"}
    data.update(changes)
    response = oauth_api.client.post("/oauth/register", json=data)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_client_metadata"


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "javascript:alert(1)",
        "data:text/html,invalid",
        "file:///tmp/callback",
        "vbscript:invalid",
        "about:blank",
        "mailto:actor@example.com",
        "https://client.example/callback#fragment",
        "https://actor:password@client.example/callback",
        "https://client.example/callback with spaces",
        "https://client.example/\\callback",
        "https://client.example/\ncallback",
        "/relative/callback",
    ],
)
def test_dynamic_registration_rejects_unsafe_redirect_uris(oauth_api, redirect_uri):
    response = oauth_api.client.post(
        "/oauth/register", json={"redirect_uris": [redirect_uri], "token_endpoint_auth_method": "none"}
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"] == "invalid_client_metadata"
    assert len(stored_rows(oauth_api, oauth_api.OAuthClient)) == 1


@pytest.mark.parametrize(
    "redirect_uri", [REDIRECT, "https://client.example/callback", "com.example.app://oauth/callback"]
)
def test_dynamic_registration_retains_safe_redirect_uri_exactly(oauth_api, redirect_uri):
    response = oauth_api.client.post(
        "/oauth/register", json={"redirect_uris": [redirect_uri], "token_endpoint_auth_method": "none"}
    )
    assert response.status_code == 201, response.text
    assert response.json()["redirect_uris"] == [redirect_uri]


def test_resource_parameter_cannot_be_repeated(oauth_api):
    params = [*authorization_params().items(), ("resource", RESOURCE)]
    auth = oauth_api.client.get("/oauth/authorize", params=params)
    assert callback_params(auth)["error"] == ["invalid_target"]
    code = issue_code(oauth_api)
    data = [*exchange_params(code).items(), ("resource", RESOURCE)]
    exchange = oauth_api.client.post(
        "/oauth/token", content=urlencode(data), headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    assert exchange.json()["error"] == "invalid_target"


@pytest.mark.parametrize("name", ["grant_type", "code", "client_id", "code_verifier"])
def test_token_rejects_identical_duplicate_parameters_before_consuming_code(oauth_api, name):
    code = issue_code(oauth_api)
    data = exchange_params(code)
    pairs = [*data.items(), (name, data[name])]
    response = oauth_api.client.post(
        "/oauth/token", content=urlencode(pairs), headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"
    assert not stored_rows(oauth_api, oauth_api.OAuthToken)
    assert oauth_api.client.post("/oauth/token", data=exchange_params(code)).status_code == 200


def test_revocation_rejects_identical_duplicate_token_before_revoking(oauth_api):
    token = issue_token(oauth_api)
    pairs = [
        ("client_id", "test-client"),
        ("token", token["access_token"]),
        ("token", token["access_token"]),
    ]
    response = oauth_api.client.post(
        "/oauth/revoke", content=urlencode(pairs), headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"
    assert oauth_api.repository.verify_access_token(token["access_token"], RESOURCE) is not None


def test_confirmation_requires_avelis_authentication_and_is_single_use(oauth_api):
    from avelis.api.deps import current_user

    ticket = issue_ticket(oauth_api)
    oauth_api.app.dependency_overrides.pop(current_user)
    assert oauth_api.client.get(f"/api/oauth/consent/{ticket}").status_code == 401
    assert oauth_api.client.post(f"/api/oauth/consent/{ticket}/approve").status_code == 401
    oauth_api.app.dependency_overrides[current_user] = lambda: oauth_api.user
    approved = oauth_api.client.post(f"/api/oauth/consent/{ticket}/approve")
    assert approved.status_code == 200, approved.text
    assert "code=" in approved.json()["redirect_to"]
    assert "iss=" in approved.json()["redirect_to"]
    assert oauth_api.client.post(f"/api/oauth/consent/{ticket}/approve").status_code == 400
    assert oauth_api.client.post(f"/api/oauth/consent/{ticket}/deny").status_code == 400


@pytest.mark.parametrize("credential", ["missing", "cookie", "mcp_token"])
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/oauth/consent/{ticket}"),
        ("POST", "/api/oauth/consent/{ticket}/approve"),
        ("POST", "/api/oauth/consent/{ticket}/deny"),
        ("GET", "/api/oauth/authorizations"),
        ("DELETE", "/api/oauth/authorizations/unknown"),
        ("GET", "/api/oauth/clients"),
        ("POST", "/api/oauth/clients/unknown/revoke"),
    ],
)
def test_confirmation_and_management_require_avelis_bearer_not_cookie_or_mcp_token(oauth_api, credential, method, path):
    from avelis.api.deps import current_user

    token = issue_token(oauth_api)
    ticket = issue_ticket(oauth_api)
    oauth_api.app.dependency_overrides.pop(current_user)
    headers = {}
    if credential == "cookie":
        headers["Cookie"] = f"access_token={token['access_token']}; session={oauth_api.user.id}"
    elif credential == "mcp_token":
        headers["Authorization"] = f"Bearer {token['access_token']}"
    response = oauth_api.client.request(method, path.format(ticket=ticket), headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_confirmation_description_lists_only_current_users_accessible_domains(oauth_api):
    ticket = issue_ticket(oauth_api, scope="metrics:read ask:execute")
    response = oauth_api.client.get(f"/api/oauth/consent/{ticket}")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["client_id"] == "test-client"
    assert data["client_name"] == "Test"
    assert data["source"] == "dynamic"
    assert {scope["scope"] for scope in data["scopes"]} == {"metrics:read", "ask:execute"}
    assert all(scope["description"] for scope in data["scopes"])
    assert data["domains"] == [{"id": "visible", "code": "visible", "name": "Visible"}]
    oauth_api.actor = oauth_api.admin
    admin_view = oauth_api.client.get(f"/api/oauth/consent/{ticket}").json()
    assert {domain["id"] for domain in admin_view["domains"]} == {"visible", "hidden"}


def test_confirmation_denial_contains_issuer_and_original_state(oauth_api):
    ticket = issue_ticket(oauth_api)
    denied = oauth_api.client.post(f"/api/oauth/consent/{ticket}/deny")
    assert denied.status_code == 200, denied.text
    params = parse_qs(urlsplit(denied.json()["redirect_to"]).query)
    assert params["error"] == ["access_denied"]
    assert params["state"] == ["test-state"]
    assert params["iss"] == [BASE]
    assert not stored_rows(oauth_api, oauth_api.OAuthAuthorizationCode, oauth_api.OAuthGrant)
    assert oauth_api.client.post(f"/api/oauth/consent/{ticket}/approve").status_code == 400


def test_confirmation_ticket_expires_and_cannot_be_used(oauth_api):
    from avelis.db import utcnow

    ticket = issue_ticket(oauth_api)
    stored = oauth_api.session.scalar(select(oauth_api.OAuthConsentTicket))
    stored.expires_at = utcnow() - timedelta(seconds=1)
    oauth_api.session.commit()
    assert oauth_api.client.get(f"/api/oauth/consent/{ticket}").status_code == 400
    assert oauth_api.client.post(f"/api/oauth/consent/{ticket}/approve").status_code == 400
    assert not stored_rows(oauth_api, oauth_api.OAuthAuthorizationCode, oauth_api.OAuthGrant)


@pytest.mark.parametrize("changes", [{"client_id": "unknown"}, {"redirect_uri": REDIRECT + "/"}])
def test_invalid_client_or_redirect_never_redirects(oauth_api, changes):
    response = oauth_api.client.get("/oauth/authorize", params=authorization_params(**changes))
    assert response.status_code in (400, 401)
    assert "location" not in response.headers
    assert not stored_rows(oauth_api, oauth_api.OAuthConsentTicket)


def test_authorization_cannot_exceed_client_allowed_scopes(oauth_api):
    client = oauth_api.session.scalar(select(oauth_api.OAuthClient))
    client.scope = "metrics:read"
    oauth_api.session.commit()
    response = oauth_api.client.get("/oauth/authorize", params=authorization_params(scope="metrics:read lineage:read"))
    params = callback_params(response)
    assert params["error"] == ["invalid_scope"]
    assert params["state"] == ["test-state"]
    assert params["iss"] == [BASE]
    assert not stored_rows(oauth_api, oauth_api.OAuthConsentTicket, oauth_api.OAuthAuthorizationCode)


@pytest.mark.parametrize("requested_scope", [None, ""])
def test_ticket_freezes_resolved_default_scope_and_preserves_explicit_empty_scope(oauth_api, requested_scope):
    stored = oauth_api.session.scalar(select(oauth_api.OAuthClient))
    stored.scope = "metrics:read"
    oauth_api.session.commit()
    ticket = issue_ticket(oauth_api, scope=requested_scope)
    shown = oauth_api.client.get(f"/api/oauth/consent/{ticket}").json()
    expected_scope = "metrics:read" if requested_scope is None else ""
    assert {scope["scope"] for scope in shown["scopes"]} == set(expected_scope.split())
    stored.scope = "metrics:read ask:execute"
    oauth_api.session.commit()
    unchanged = oauth_api.client.get(f"/api/oauth/consent/{ticket}").json()
    assert unchanged["scopes"] == shown["scopes"]
    approved = oauth_api.client.post(f"/api/oauth/consent/{ticket}/approve")
    assert approved.status_code == 200, approved.text
    code = parse_qs(urlsplit(approved.json()["redirect_to"]).query)["code"][0]
    response = oauth_api.client.post("/oauth/token", data=exchange_params(code))
    assert response.status_code == 200, response.text
    assert response.json().get("scope", "") == expected_scope
    token = oauth_api.repository.verify_access_token(response.json()["access_token"], RESOURCE)
    assert token is not None and token.scope == expected_scope
    assert oauth_api.session.scalar(select(oauth_api.OAuthGrant)).scope == expected_scope


def test_authorization_is_bound_to_user_approving_ticket(oauth_api):
    ticket = issue_ticket(oauth_api)
    oauth_api.actor = oauth_api.other
    approved = oauth_api.client.post(f"/api/oauth/consent/{ticket}/approve")
    code = parse_qs(urlsplit(approved.json()["redirect_to"]).query)["code"][0]
    response = oauth_api.client.post("/oauth/token", data=exchange_params(code))
    token = response.json()
    assert oauth_api.repository.verify_access_token(token["access_token"], RESOURCE).user_id == oauth_api.other.id
    grant = oauth_api.session.scalar(select(oauth_api.OAuthGrant))
    assert grant.user_id == oauth_api.other.id


def test_authorizations_list_and_revocation_are_owned_by_current_user(oauth_api):
    api = oauth_api
    token = issue_token(api)
    response = api.client.get("/api/oauth/authorizations")
    assert response.status_code == 200, response.text
    grants = response.json()
    assert len(grants) == 1
    assert grants[0]["client_id"] == "test-client"
    api.actor = api.other
    assert api.client.get("/api/oauth/authorizations").json() == []
    assert api.client.delete(f"/api/oauth/authorizations/{grants[0]['id']}").status_code == 404
    assert api.repository.verify_access_token(token["access_token"], RESOURCE) is not None
    api.actor = api.user
    assert api.client.delete(f"/api/oauth/authorizations/{grants[0]['id']}").status_code in (200, 204)
    assert api.repository.verify_access_token(token["access_token"], RESOURCE) is None
    assert refresh(api, token).json()["error"] == "invalid_grant"


def test_revoking_grant_invalidates_all_its_rotated_tokens(oauth_api):
    token = issue_token(oauth_api)
    refreshed = refresh(oauth_api, token).json()
    grant = oauth_api.client.get("/api/oauth/authorizations").json()[0]
    response = oauth_api.client.delete(f"/api/oauth/authorizations/{grant['id']}")
    assert response.status_code in (200, 204)
    assert oauth_api.repository.verify_access_token(refreshed["access_token"], RESOURCE) is None
    assert refresh(oauth_api, refreshed).json()["error"] == "invalid_grant"


def test_client_management_requires_admin_and_disable_revokes_all_users(oauth_api):
    api = oauth_api
    first = issue_token(api)
    api.actor = api.other
    second = issue_token(api)
    assert api.client.get("/api/oauth/clients").status_code == 403
    client_record = api.session.scalar(select(api.OAuthClient))
    assert api.client.post(f"/api/oauth/clients/{client_record.id}/revoke").status_code == 403
    api.actor = api.admin
    response = api.client.get("/api/oauth/clients")
    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1 and rows[0]["client_id"] == "test-client"
    assert rows[0]["source"] == "dynamic"
    assert rows[0]["authorization_count"] == 2
    assert "last_used_at" in rows[0]
    response = api.client.post(f"/api/oauth/clients/{rows[0]['id']}/revoke")
    assert response.status_code in (200, 204), response.text
    for token in (first, second):
        assert api.repository.verify_access_token(token["access_token"], RESOURCE) is None
        assert refresh(api, token).json()["error"] in ("invalid_client", "invalid_grant")
    response = api.client.get("/oauth/authorize", params=authorization_params())
    assert response.status_code in (400, 401) and "location" not in response.headers


def test_oauth_audit_records_actions_without_credentials(oauth_api):
    api = oauth_api
    registered = api.client.post(
        "/oauth/register",
        json={"client_name": "Audit", "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "client_secret_post"},
    )
    assert registered.status_code == 201, registered.text
    ticket = issue_ticket(api)
    approved = api.client.post(f"/api/oauth/consent/{ticket}/approve")
    code = parse_qs(urlsplit(approved.json()["redirect_to"]).query)["code"][0]
    token = api.client.post("/oauth/token", data=exchange_params(code)).json()
    grant = api.client.get("/api/oauth/authorizations").json()[0]
    api.client.delete(f"/api/oauth/authorizations/{grant['id']}")
    api.actor = api.admin
    row = next(row for row in api.client.get("/api/oauth/clients").json() if row["client_id"] == "test-client")
    api.client.post(f"/api/oauth/clients/{row['id']}/revoke")
    events = list(api.session.scalars(select(api.AuditEvent)))
    actions = {event.action for event in events}
    assert {"oauth.client_register", "oauth.authorize", "oauth.revoke", "oauth.client_disable"} <= actions
    details = repr([event.detail for event in events])
    for secret in (ticket, code, token["access_token"], token["refresh_token"], registered.json()["client_secret"]):
        assert secret not in details
    approval = next(event for event in events if event.action == "oauth.authorize")
    assert approval.actor_id == api.user.id


def test_cimd_client_is_persisted_cached_and_refetched_after_ttl(oauth_api, monkeypatch):
    from avelis.db import utcnow
    from avelis.oauth import router

    client_id = "https://client.example/oauth.json"
    calls = []

    async def fetch(url, **kwargs):
        calls.append((url, kwargs))
        return {"client_id": url, "client_name": f"CIMD {len(calls)}", "redirect_uris": [REDIRECT]}

    monkeypatch.setattr(router, "fetch_client_metadata", fetch)
    ticket = issue_ticket(oauth_api, client_id=client_id)
    description = oauth_api.client.get(f"/api/oauth/consent/{ticket}").json()
    assert description["source"] == "cimd"
    assert description["client_name"] == "CIMD 1"
    stored = oauth_api.session.scalar(select(oauth_api.OAuthClient).where(oauth_api.OAuthClient.client_id == client_id))
    assert stored.metadata_hash
    assert 3590 <= (stored.metadata_expires_at - stored.metadata_fetched_at).total_seconds() <= 3600
    issue_ticket(oauth_api, client_id=client_id)
    assert len(calls) == 1
    stored.metadata_expires_at = utcnow() - timedelta(seconds=1)
    oauth_api.session.commit()
    second_ticket = issue_ticket(oauth_api, client_id=client_id)
    second = oauth_api.client.get(f"/api/oauth/consent/{second_ticket}").json()
    assert len(calls) == 2
    assert second["client_name"] == "CIMD 2"
    assert len(stored_rows(oauth_api, oauth_api.OAuthClient)) == 2


def test_disabled_cimd_client_cannot_be_refetched_or_reenabled(oauth_api, monkeypatch):
    from avelis.db import utcnow
    from avelis.oauth import router

    client_id = "https://client.example/oauth.json"
    calls = []

    async def fetch(url, **kwargs):
        calls.append(url)
        return {"client_id": url, "redirect_uris": [REDIRECT]}

    monkeypatch.setattr(router, "fetch_client_metadata", fetch)
    ticket = issue_ticket(oauth_api, client_id=client_id)
    stored = oauth_api.session.scalar(select(oauth_api.OAuthClient).where(oauth_api.OAuthClient.client_id == client_id))
    oauth_api.actor = oauth_api.admin
    assert oauth_api.client.post(f"/api/oauth/clients/{stored.id}/revoke").status_code == 200
    stored.metadata_expires_at = utcnow() - timedelta(seconds=1)
    oauth_api.session.commit()
    response = oauth_api.client.get("/oauth/authorize", params=authorization_params(client_id=client_id))
    assert response.status_code in (400, 401)
    assert "location" not in response.headers
    assert len(calls) == 1
    assert oauth_api.client.post(f"/api/oauth/consent/{ticket}/approve").status_code == 400


def test_expired_cimd_cache_is_not_reused_when_refetch_fails(oauth_api, monkeypatch):
    from avelis.db import utcnow
    from avelis.oauth import router

    client_id = "https://client.example/oauth.json"

    async def fetch(url, **kwargs):
        return {"client_id": url, "redirect_uris": [REDIRECT]}

    monkeypatch.setattr(router, "fetch_client_metadata", fetch)
    issue_ticket(oauth_api, client_id=client_id)
    stored = oauth_api.session.scalar(select(oauth_api.OAuthClient).where(oauth_api.OAuthClient.client_id == client_id))
    stored.metadata_expires_at = utcnow() - timedelta(seconds=1)
    oauth_api.session.commit()

    async def unavailable(url, **kwargs):
        raise router.CIMDError("unavailable")

    monkeypatch.setattr(router, "fetch_client_metadata", unavailable)
    response = oauth_api.client.get("/oauth/authorize", params=authorization_params(client_id=client_id))
    assert response.status_code in (400, 401)
    assert "location" not in response.headers
    assert len(stored_rows(oauth_api, oauth_api.OAuthConsentTicket)) == 1


@pytest.mark.parametrize("change", ["scope", "redirect_uri"])
@pytest.mark.parametrize("decision", ["view", "approve", "deny"])
def test_metadata_changes_safely_invalidate_existing_confirmation(oauth_api, change, decision):
    ticket = issue_ticket(oauth_api)
    stored = oauth_api.session.scalar(select(oauth_api.OAuthClient))
    if change == "scope":
        stored.scope = "lineage:read"
    else:
        stored.redirect_uris = [REDIRECT + "/new"]
    oauth_api.session.commit()
    if decision == "view":
        response = oauth_api.client.get(f"/api/oauth/consent/{ticket}")
    else:
        response = oauth_api.client.post(f"/api/oauth/consent/{ticket}/{decision}")
    assert response.status_code == 400
    assert "location" not in response.headers
    assert not stored_rows(oauth_api, oauth_api.OAuthGrant, oauth_api.OAuthAuthorizationCode)
    assert oauth_api.session.scalar(select(oauth_api.OAuthConsentTicket)).consumed_at is None


def test_wrong_verifier_cannot_revoke_family_using_consumed_authorization_code(oauth_api):
    code = issue_code(oauth_api)
    token = oauth_api.client.post("/oauth/token", data=exchange_params(code)).json()
    response = oauth_api.client.post("/oauth/token", data=exchange_params(code, code_verifier="b" * 64))
    assert response.status_code == 400 and response.json()["error"] == "invalid_grant"
    assert oauth_api.repository.verify_access_token(token["access_token"], RESOURCE) is not None


@pytest.mark.parametrize("credential", ["code", "refresh"])
def test_another_client_cannot_revoke_family_using_its_consumed_credential(oauth_api, credential):
    code = issue_code(oauth_api)
    token = oauth_api.client.post("/oauth/token", data=exchange_params(code)).json()
    refreshed = refresh(oauth_api, token).json()
    oauth_api.session.add(
        oauth_api.OAuthClient(
            client_id="other-client",
            client_name="Other",
            redirect_uris=[REDIRECT],
            scope=SCOPES,
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
            source="dynamic",
        )
    )
    oauth_api.session.commit()
    if credential == "code":
        response = oauth_api.client.post("/oauth/token", data=exchange_params(code, client_id="other-client"))
    else:
        response = refresh(oauth_api, token, client_id="other-client")
    assert response.status_code == 400 and response.json()["error"] == "invalid_grant"
    assert oauth_api.repository.verify_access_token(refreshed["access_token"], RESOURCE) is not None


def test_authlib_debug_logging_cannot_disclose_credentials(oauth_api, caplog):
    import logging

    with caplog.at_level(logging.DEBUG):
        token = issue_token(oauth_api)
        refreshed = refresh(oauth_api, token).json()
    assert logging.getLogger("authlib").getEffectiveLevel() >= logging.INFO
    authlib_logs = "\n".join(record.getMessage() for record in caplog.records if record.name.startswith("authlib"))
    for credential in (
        token["access_token"],
        token["refresh_token"],
        refreshed["access_token"],
        refreshed["refresh_token"],
    ):
        assert credential not in authlib_logs
