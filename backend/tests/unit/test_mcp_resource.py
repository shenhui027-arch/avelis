"""MCP 资源服务器：真实数据库凭据与 SDK 2.3 无状态 HTTP 边界。"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp_types import CLIENT_CAPABILITIES_META_KEY, CLIENT_INFO_META_KEY, PROTOCOL_VERSION_META_KEY
from sqlalchemy import JSON, MetaData, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from avelis.config import Settings
from avelis.models import AuditEvent, OAuthClient, OAuthGrant, OAuthToken, User
from avelis.oauth.repository import SCOPES, credential_hash

BASE = "https://mcp.example"
RESOURCE = BASE + "/mcp"
RAW_TOKEN = "resource-boundary-test-only-opaque-token"
TOOL_SCOPES = {
    "search_metrics": "metrics:read",
    "get_metric": "metrics:read",
    "ask_metric": "ask:execute",
    "lineage_neighbors": "lineage:read",
    "list_changes": "lineage:read",
}


@pytest.fixture
def resource_app():
    from avelis.mcp.server import create_mcp_app, protected_resource_routes

    settings = Settings(
        database_url="sqlite://",
        redis_url="redis://unused",
        secret="resource-boundary-secret",
        public_url=BASE,
        mcp_enabled=True,
    )
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in (User, OAuthClient, OAuthGrant, OAuthToken, AuditEvent):
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(engine)
    maker = sessionmaker(engine, expire_on_commit=False)
    with maker.begin() as session:
        session.add(User(id="boundary-user", username="boundary-user", display_name="边界用户", password_hash="unused"))
        session.add(
            OAuthClient(
                id="boundary-client-record",
                client_id="boundary-client",
                client_name="Boundary client",
                source="dynamic",
                redirect_uris=["https://client.example/callback"],
                scope=" ".join(SCOPES),
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
            )
        )
        session.add(
            OAuthGrant(
                id="boundary-grant",
                client_record_id="boundary-client-record",
                user_id="boundary-user",
                scope=" ".join(SCOPES),
            )
        )
    child = create_mcp_app(settings, session_maker=maker)

    @asynccontextmanager
    async def lifespan(_app):
        async with child.router.lifespan_context(child):
            yield

    app = FastAPI(lifespan=lifespan)
    app.router.routes.extend(protected_resource_routes(settings))
    app.mount("/", child)
    try:
        with TestClient(app, base_url=BASE) as client:
            yield SimpleNamespace(client=client, maker=maker, settings=settings)
    finally:
        engine.dispose()


def seed_token(state, **changes):
    moment = datetime.now(timezone.utc)
    fields = dict(
        id="boundary-token",
        access_token_hash=credential_hash(RAW_TOKEN),
        refresh_token_hash=None,
        client_record_id="boundary-client-record",
        client_id="boundary-client",
        user_id="boundary-user",
        grant_id="boundary-grant",
        scope=" ".join(SCOPES),
        resource=RESOURCE,
        family_id="boundary-family",
        issued_at=moment,
        expires_at=moment + timedelta(seconds=900),
    )
    fields.update(changes)
    with state.maker.begin() as session:
        record = OAuthToken(**fields)
        session.add(record)
    return record


def modern_message(method="tools/list", *, name=None, arguments=None):
    params = {
        "_meta": {
            PROTOCOL_VERSION_META_KEY: "2026-07-28",
            CLIENT_CAPABILITIES_META_KEY: {},
            CLIENT_INFO_META_KEY: {"name": "resource-boundary-tests", "version": "1"},
        }
    }
    if name is not None:
        params.update(name=name, arguments=arguments or {})
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


def request_headers(method="tools/list", *, name=None, authorized=True):
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": method,
    }
    if name is not None:
        headers["Mcp-Name"] = name
    if authorized:
        headers["Authorization"] = "Bearer " + RAW_TOKEN
    return headers


def call(state, name, arguments=None):
    return state.client.post(
        "/mcp",
        json=modern_message("tools/call", name=name, arguments=arguments),
        headers=request_headers("tools/call", name=name),
    )


def test_protected_resource_metadata(resource_app):
    response = resource_app.client.get("/.well-known/oauth-protected-resource/mcp")
    assert response.status_code == 200
    metadata = response.json()
    assert metadata["resource"] == RESOURCE
    assert metadata["authorization_servers"] == [BASE]
    assert set(metadata["scopes_supported"]) == set(SCOPES)
    assert metadata["bearer_methods_supported"] == ["header"]


@pytest.mark.parametrize("authorization", [None, "Bearer unknown", "Basic unused"])
def test_unauthenticated_requests_are_challenged(resource_app, authorization):
    headers = request_headers(authorized=False)
    if authorization:
        headers["Authorization"] = authorization
    response = resource_app.client.post("/mcp", json=modern_message(), headers=headers)
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_token"
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"' in challenge
    assert "Mcp-Session-Id" not in response.headers


@pytest.mark.parametrize(
    "resource",
    [
        "",
        BASE + "/other",
        RESOURCE + "/",
        RESOURCE + "?audience=other",
        "https://other.example/mcp",
        "https://MCP.example/mcp",
    ],
)
def test_resource_is_an_exact_configured_uri(resource_app, resource):
    seed_token(resource_app, resource=resource)
    response = resource_app.client.post("/mcp", json=modern_message(), headers=request_headers())
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_token"


@pytest.mark.parametrize(
    "state", ["expired", "revoked", "grant_revoked", "client_disabled", "user_disabled", "password_reset"]
)
def test_database_credential_revocations_are_enforced(resource_app, state):
    moment = datetime.now(timezone.utc)
    seed_token(resource_app)
    with resource_app.maker.begin() as session:
        if state == "expired":
            session.get(OAuthToken, "boundary-token").expires_at = moment - timedelta(seconds=1)
        elif state == "revoked":
            session.get(OAuthToken, "boundary-token").access_revoked_at = moment
        elif state == "grant_revoked":
            session.get(OAuthGrant, "boundary-grant").revoked_at = moment
        elif state == "client_disabled":
            session.get(OAuthClient, "boundary-client-record").disabled_at = moment
        elif state == "user_disabled":
            session.get(User, "boundary-user").is_active = False
        else:
            session.get(User, "boundary-user").sessions_valid_after = moment + timedelta(seconds=1)
    response = resource_app.client.post("/mcp", json=modern_message(), headers=request_headers())
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_token"


def test_expiry_is_rejected_at_the_exact_boundary(resource_app, monkeypatch):
    expiry = datetime.now(timezone.utc) + timedelta(minutes=15)
    seed_token(resource_app, expires_at=expiry)
    monkeypatch.setattr("avelis.oauth.repository.now", lambda: expiry)
    response = resource_app.client.post("/mcp", json=modern_message(), headers=request_headers())
    assert response.status_code == 401


def test_tools_list_needs_no_initialization_or_session(resource_app):
    seed_token(resource_app)
    response = resource_app.client.post("/mcp", json=modern_message(), headers=request_headers())
    assert response.status_code == 200, response.text
    assert "Mcp-Session-Id" not in response.headers
    tools = response.json()["result"]["tools"]
    assert {tool["name"] for tool in tools} == set(TOOL_SCOPES)
    ask = next(tool for tool in tools if tool["name"] == "ask_metric")
    assert "sql" not in ask["inputSchema"].get("properties", {})


@pytest.mark.parametrize("name,required", TOOL_SCOPES.items())
def test_each_tool_rejects_other_scopes_with_http_challenge(resource_app, name, required):
    seed_token(resource_app, scope=" ".join(scope for scope in SCOPES if scope != required))
    response = call(resource_app, name)
    assert response.status_code == 403, response.text
    assert response.json()["error"] == "insufficient_scope"
    challenge = response.headers["www-authenticate"]
    assert 'error="insufficient_scope"' in challenge
    assert f'scope="{required}"' in challenge
    assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"' in challenge


@pytest.mark.parametrize("name,scope", TOOL_SCOPES.items())
def test_each_tool_accepts_its_own_scope(resource_app, monkeypatch, name, scope):
    from avelis.mcp.tools import ToolService

    arguments = {
        "search_metrics": {"keyword": "private-query-marker"},
        "get_metric": {"metric": "boundary-code"},
        "ask_metric": {"metric": "boundary-code", "start_date": "2026-01-01", "end_date": "2026-01-02"},
        "lineage_neighbors": {"asset_id": "boundary-asset"},
        "list_changes": {"object_type": "asset", "object_id": "boundary-asset"},
    }[name]
    seen = []
    private_result = "private-result-row-marker"

    def stub(service, **kwargs):
        seen.append((service.user.id, kwargs))
        rows = [{"value": private_result}]
        return rows if name in ("search_metrics", "list_changes") else {"rows": rows}

    monkeypatch.setattr(ToolService, name, stub)
    seed_token(resource_app, scope=scope)
    response = call(resource_app, name, arguments)
    assert response.status_code == 200, response.text
    assert response.json()["result"]["isError"] is False, response.text
    assert "Mcp-Session-Id" not in response.headers
    assert len(seen) == 1 and seen[0][0] == "boundary-user"
    with resource_app.maker() as session:
        events = list(session.scalars(select(AuditEvent)))
        assert len(events) == 1
        assert events[0].action == "mcp." + name
        assert events[0].detail["token_id"] == "boundary-token"
        snapshot = repr(events[0].detail)
    assert RAW_TOKEN not in snapshot
    assert private_result not in snapshot
    assert "private-query-marker" not in snapshot


@pytest.mark.parametrize("version", [None, "2025-11-25", "2025-06-18"])
def test_legacy_and_missing_protocol_headers_are_rejected(resource_app, version):
    seed_token(resource_app)
    headers = request_headers()
    if version is None:
        headers.pop("MCP-Protocol-Version")
    else:
        headers["MCP-Protocol-Version"] = version
    response = resource_app.client.post("/mcp", json=modern_message(), headers=headers)
    assert response.status_code == 400
    assert "Mcp-Session-Id" not in response.headers


@pytest.mark.parametrize(
    "extra_headers,status",
    [
        ({"Host": "untrusted.example"}, 421),
        ({"Origin": "https://untrusted.example"}, 403),
    ],
)
def test_transport_rejects_untrusted_host_and_origin(resource_app, extra_headers, status):
    seed_token(resource_app)
    response = resource_app.client.post(
        "/mcp",
        json=modern_message(),
        headers={**request_headers(), **extra_headers},
    )
    assert response.status_code == status


def test_oversized_body_is_rejected_before_scope_gate_buffers_it(resource_app):
    from avelis.mcp.auth import MAX_BODY_BYTES

    seed_token(resource_app)
    response = resource_app.client.post(
        "/mcp",
        content=b" " * (MAX_BODY_BYTES + 1),
        headers={**request_headers(), "Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_header_name_mismatch_cannot_select_a_different_tool(resource_app):
    seed_token(resource_app, scope="metrics:read")
    response = resource_app.client.post(
        "/mcp",
        json=modern_message("tools/call", name="ask_metric"),
        headers=request_headers("tools/call", name="search_metrics"),
    )
    assert response.status_code == 403
    assert 'scope="ask:execute"' in response.headers["www-authenticate"]


def test_header_method_mismatch_cannot_hide_a_tool_call(resource_app):
    seed_token(resource_app, scope="metrics:read")
    response = resource_app.client.post(
        "/mcp",
        json=modern_message("tools/call", name="ask_metric"),
        headers=request_headers("tools/list"),
    )
    assert response.status_code == 403
    assert 'scope="ask:execute"' in response.headers["www-authenticate"]


def test_jsonrpc_batch_cannot_bypass_tool_scope_gate(resource_app):
    seed_token(resource_app, scope="metrics:read")
    response = resource_app.client.post(
        "/mcp",
        json=[modern_message("tools/call", name="ask_metric")],
        headers=request_headers("tools/call", name="ask_metric"),
    )
    assert response.status_code == 400, response.text
    assert "result" not in response.json()


def test_sql_cannot_be_smuggled_as_an_extra_tool_argument(resource_app, monkeypatch):
    from avelis.mcp.tools import ToolService

    invoked = []
    monkeypatch.setattr(ToolService, "ask_metric", lambda *args, **kwargs: invoked.append(kwargs))
    seed_token(resource_app, scope="ask:execute")
    response = call(
        resource_app,
        "ask_metric",
        {
            "metric": "boundary-code",
            "start_date": "2026-01-01",
            "end_date": "2026-01-02",
            "sql": "SELECT private_column FROM private_table",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"
    assert not invoked


def test_rejected_call_audit_uses_internal_identity_and_argument_summary(resource_app):
    seed_token(resource_app, scope="ask:execute")
    sensitive = "never-retain-this-argument-value"
    response = call(
        resource_app,
        "ask_metric",
        {
            "metric": sensitive,
            "filters": [{"field": "private", "value": sensitive}],
            sensitive: RAW_TOKEN,
        },
    )
    assert response.status_code == 400
    with resource_app.maker() as session:
        event = session.scalar(select(AuditEvent))
        assert event.actor_id == "boundary-user"
        assert event.action == "mcp.ask_metric"
        assert event.detail["client_id"] == "boundary-client"
        assert event.detail["token_id"] == "boundary-token"
        summary = event.detail["parameters"]
        assert summary["argument_names"] == ["filters", "metric"]
        assert summary["unknown_argument_count"] == 1
        assert summary["filters_count"] == 1
        snapshot = repr(event.detail)
    assert sensitive not in snapshot
    assert RAW_TOKEN not in snapshot
    assert credential_hash(RAW_TOKEN) not in snapshot


def test_database_retains_only_credential_hashes(resource_app):
    seed_token(resource_app)
    with resource_app.maker() as session:
        token = session.scalar(select(OAuthToken))
        snapshot = repr({column.name: getattr(token, column.name) for column in OAuthToken.__table__.columns})
    assert RAW_TOKEN not in snapshot
    assert credential_hash(RAW_TOKEN) in snapshot


async def test_verifier_returns_internal_token_identity_and_issuer(resource_app):
    from avelis.mcp.auth import StoredTokenVerifier

    seed_token(resource_app)
    token = await StoredTokenVerifier(resource_app.maker, resource_app.settings).verify_token(RAW_TOKEN)
    assert token is not None
    assert token.subject == "boundary-user"
    assert token.client_id == "boundary-client"
    assert set(token.scopes) == set(SCOPES)
    assert token.resource == RESOURCE
    assert token.claims["token_id"] == "boundary-token"
    assert token.claims["iss"] == BASE


def test_tool_call_updates_client_and_grant_recent_use_but_listing_does_not(resource_app, monkeypatch):
    from avelis.mcp.tools import ToolService

    seed_token(resource_app)
    monkeypatch.setattr(ToolService, "search_metrics", lambda self, **kwargs: [])
    listing = resource_app.client.post("/mcp", json=modern_message(), headers=request_headers())
    assert listing.status_code == 200
    with resource_app.maker() as session:
        assert session.get(OAuthClient, "boundary-client-record").last_used_at is None
        assert session.get(OAuthGrant, "boundary-grant").last_used_at is None
    assert call(resource_app, "search_metrics").status_code == 200
    with resource_app.maker() as session:
        client = session.get(OAuthClient, "boundary-client-record")
        grant = session.get(OAuthGrant, "boundary-grant")
        assert client.last_used_at is not None
        assert client.last_used_at == grant.last_used_at
        assert len(list(session.scalars(select(AuditEvent)))) == 1


@pytest.mark.parametrize("suffix", ["/", "/prefix"])
def test_public_url_path_preserves_issuer_resource_but_origin_is_the_origin(resource_app, suffix):
    from dataclasses import replace

    from avelis.mcp.auth import StoredTokenVerifier
    from avelis.mcp.server import create_mcp_app

    settings = replace(resource_app.settings, public_url=BASE + suffix)
    resource = settings.public_url + "/mcp"
    seed_token(resource_app, resource=resource)
    child = create_mcp_app(settings, session_maker=resource_app.maker)

    @asynccontextmanager
    async def lifespan(_app):
        async with child.router.lifespan_context(child):
            yield

    app = FastAPI(lifespan=lifespan)
    app.mount("/", child)
    with TestClient(app, base_url=BASE) as client:
        response = client.post(
            "/mcp",
            json=modern_message(),
            headers={**request_headers(), "Origin": BASE},
        )
    assert response.status_code == 200, response.text
    # HTTP Origin 去掉路径；资源与 issuer 仍必须使用管理员配置的精确原文。
    import asyncio

    token = asyncio.run(StoredTokenVerifier(resource_app.maker, settings).verify_token(RAW_TOKEN))
    assert token.claims["iss"] == settings.public_url
    assert token.resource == resource
