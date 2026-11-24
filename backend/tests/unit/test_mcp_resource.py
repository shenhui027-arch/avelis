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
def resource_app(request):
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
        with TestClient(app, base_url=BASE, root_path=getattr(request, "param", "")) as client:
            yield SimpleNamespace(client=client, maker=maker, settings=settings, child=child)
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


@pytest.mark.parametrize("resource_app", ["/prefix"], indirect=True)
@pytest.mark.parametrize("name,required", TOOL_SCOPES.items())
def test_prefixed_tool_scope_rejection_is_audited(resource_app, name, required):
    seed_token(resource_app, scope=" ".join(scope for scope in SCOPES if scope != required))
    response = resource_app.client.post(
        "/prefix/mcp",
        json=modern_message("tools/call", name=name),
        headers=request_headers("tools/call", name=name),
    )
    assert response.status_code == 403, response.text
    assert response.json() == {"error": "insufficient_scope", "error_description": f"Required scope: {required}"}
    assert f'scope="{required}"' in response.headers["www-authenticate"]
    with resource_app.maker() as session:
        events = list(session.scalars(select(AuditEvent)))
        assert len(events) == 1
        assert events[0].action == "mcp." + name
        assert events[0].actor_id == "boundary-user"
        assert events[0].detail["token_id"] == "boundary-token"


@pytest.mark.parametrize("resource_app", ["/prefix"], indirect=True)
@pytest.mark.parametrize("name,scope", TOOL_SCOPES.items())
def test_prefixed_tool_call_succeeds_and_is_audited_once(resource_app, monkeypatch, name, scope):
    from avelis.mcp.tools import ToolService

    seen = []

    def stub(self, **kwargs):
        seen.append(self.user.id)
        return [] if name in ("search_metrics", "list_changes") else {}

    arguments = {
        "search_metrics": {},
        "get_metric": {"metric": "boundary-code"},
        "ask_metric": {"metric": "boundary-code", "start_date": "2026-01-01", "end_date": "2026-01-02"},
        "lineage_neighbors": {"asset_id": "boundary-asset"},
        "list_changes": {"object_type": "asset", "object_id": "boundary-asset"},
    }[name]
    seed_token(resource_app, scope=scope)
    monkeypatch.setattr(ToolService, name, stub)
    response = resource_app.client.post(
        "/prefix/mcp",
        json=modern_message("tools/call", name=name, arguments=arguments),
        headers=request_headers("tools/call", name=name),
    )
    assert response.status_code == 200, response.text
    assert response.json()["result"]["isError"] is False, response.text
    assert seen == ["boundary-user"]
    with resource_app.maker() as session:
        events = list(session.scalars(select(AuditEvent)))
        assert len(events) == 1
        assert events[0].action == "mcp." + name


@pytest.mark.parametrize("name,required", TOOL_SCOPES.items())
@pytest.mark.parametrize("allowed", [False, True])
async def test_direct_tool_wrapper_checks_scope_and_audits(resource_app, monkeypatch, name, required, allowed):
    import json

    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.mcpserver.exceptions import ToolError

    from avelis.mcp.auth import StoredTokenVerifier, audited_tool_call
    from avelis.mcp.tools import ToolService

    seed_token(resource_app, scope=required if allowed else " ".join(s for s in SCOPES if s != required))
    token = await StoredTokenVerifier(resource_app.maker, resource_app.settings).verify_token(RAW_TOKEN)
    seen = []
    monkeypatch.setattr(ToolService, name, lambda self, **kwargs: seen.append(kwargs) or [])
    wrapper = resource_app.child.state.mcp_server._tool_manager.get_tool(name).fn
    context = auth_context_var.set(AuthenticatedUser(token))
    try:
        assert audited_tool_call.get() is None
        if allowed:
            assert wrapper(keyword="private-marker") == []
            assert seen == [{"keyword": "private-marker"}]
        else:
            with pytest.raises(ToolError) as error:
                wrapper(keyword="private-marker")
            assert json.loads(str(error.value)) == {
                "error": "insufficient_scope",
                "error_description": f"Required scope: {required}",
            }
            assert seen == []
    finally:
        auth_context_var.reset(context)
    with resource_app.maker() as session:
        events = list(session.scalars(select(AuditEvent)))
        assert len(events) == 1
        assert events[0].action == "mcp." + name
        assert events[0].actor_id == "boundary-user"
        assert events[0].detail["client_id"] == "boundary-client"
        assert events[0].detail["token_id"] == "boundary-token"
        assert "private-marker" not in repr(events[0].detail)
        assert RAW_TOKEN not in repr(events[0].detail)


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


@pytest.mark.parametrize("version", [None, "2025-03-26", "2025-06-18", "2025-11-25"])
def test_legacy_and_missing_protocol_headers_are_accepted(resource_app, version):
    seed_token(resource_app)
    headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": "Bearer " + RAW_TOKEN,
    }
    if version is not None:
        headers["MCP-Protocol-Version"] = version
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    response = resource_app.client.post("/mcp", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    assert "Mcp-Session-Id" not in response.headers
    assert response.headers.get("mcp-protocol-version") == (version or "2025-03-26")
    assert {tool["name"] for tool in response.json()["result"]["tools"]} == set(TOOL_SCOPES)


@pytest.mark.parametrize(
    "requested,expected",
    [
        ("2025-03-26", "2025-03-26"),
        ("2025-06-18", "2025-06-18"),
        ("2025-11-25", "2025-11-25"),
        ("2099-01-01", "2025-11-25"),
        ("2026-07-28", "2025-11-25"),
    ],
)
def test_initialize_negotiates_protocol_version(resource_app, requested, expected):
    seed_token(resource_app)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": requested,
            "capabilities": {},
            "clientInfo": {"name": "legacy-client", "version": "1.0"},
        },
    }
    response = resource_app.client.post(
        "/mcp",
        json=payload,
        headers={
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + RAW_TOKEN,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["result"]["protocolVersion"] == expected
    assert response.headers.get("mcp-protocol-version") == expected


def test_initialize_2024_11_05_does_not_stamp_contradictory_header(resource_app):
    seed_token(resource_app)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "legacy-client", "version": "1.0"},
        },
    }
    response = resource_app.client.post(
        "/mcp",
        json=payload,
        headers={
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + RAW_TOKEN,
        },
    )
    assert response.status_code == 200, response.text
    assert "mcp-protocol-version" not in response.headers


@pytest.mark.parametrize("version", [None, "2025-03-26", "2025-06-18", "2025-11-25"])
def test_legacy_tools_call_enforces_scope_args_and_audits(resource_app, monkeypatch, version):
    from avelis.mcp.tools import ToolService

    seen = []
    monkeypatch.setattr(ToolService, "search_metrics", lambda self, **kwargs: seen.append(kwargs) or [])
    seed_token(resource_app, scope="metrics:read")
    headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": "Bearer " + RAW_TOKEN,
    }
    if version is not None:
        headers["MCP-Protocol-Version"] = version
    expected_header = version or "2025-03-26"

    ok_resp = resource_app.client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "search_metrics", "arguments": {"keyword": "rev"}},
        },
        headers=headers,
    )
    assert ok_resp.status_code == 200, ok_resp.text
    assert ok_resp.headers.get("mcp-protocol-version") == expected_header
    assert seen == [{"keyword": "rev", "domain_id": None, "limit": 50}]

    denied_resp = resource_app.client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "ask_metric", "arguments": {"metric": "m1"}},
        },
        headers=headers,
    )
    assert denied_resp.status_code == 403
    assert denied_resp.headers.get("mcp-protocol-version") == expected_header
    assert 'scope="ask:execute"' in denied_resp.headers["www-authenticate"]

    seed_token(
        resource_app, id="boundary-token-2", access_token_hash=credential_hash("raw-token-2"), scope="ask:execute"
    )
    bad_arg_resp = resource_app.client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "ask_metric", "arguments": {"metric": "m1", "sql": "SELECT 1"}},
        },
        headers={**headers, "Authorization": "Bearer raw-token-2"},
    )
    assert bad_arg_resp.status_code == 400
    assert bad_arg_resp.json()["error"] == "invalid_request"

    with resource_app.maker() as session:
        events = list(session.scalars(select(AuditEvent)))
        assert len(events) == 3
        assert [e.action for e in events] == ["mcp.search_metrics", "mcp.ask_metric", "mcp.ask_metric"]


@pytest.mark.parametrize("method", ["POST", "GET", "DELETE"])
@pytest.mark.parametrize("version", ["", "   ", "2024-11-05", "2025-01-01", "2099-01-01", "invalid"])
def test_unsupported_protocol_headers_are_rejected(resource_app, method, version):
    seed_token(resource_app)
    headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": "Bearer " + RAW_TOKEN,
        "MCP-Protocol-Version": version,
    }
    kwargs = {"headers": headers}
    if method == "POST":
        kwargs["json"] = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    response = resource_app.client.request(method, "/mcp", **kwargs)
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "unsupported_protocol_version"
    assert body["supported_versions"] == ["2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28"]
    assert "Mcp-Session-Id" not in response.headers


@pytest.mark.parametrize("method", ["POST", "GET", "DELETE"])
def test_duplicate_protocol_headers_are_rejected(resource_app, method):
    seed_token(resource_app)
    headers = [
        ("accept", "application/json, text/event-stream"),
        ("authorization", "Bearer " + RAW_TOKEN),
        ("mcp-protocol-version", "2025-11-25"),
        ("mcp-protocol-version", "2026-07-28"),
    ]
    kwargs = {"headers": headers}
    if method == "POST":
        kwargs["json"] = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    response = resource_app.client.request(method, "/mcp", **kwargs)
    assert response.status_code == 400
    assert response.json()["error"] == "unsupported_protocol_version"


@pytest.mark.parametrize("version", [None, "2025-11-25"])
def test_get_and_delete_legacy_and_missing_headers_reach_transport(resource_app, version):
    seed_token(resource_app)
    base_headers = {"Authorization": "Bearer " + RAW_TOKEN}
    if version is not None:
        base_headers["MCP-Protocol-Version"] = version
    expected = version or "2025-03-26"

    del_resp = resource_app.client.delete("/mcp", headers=base_headers)
    assert del_resp.status_code == 405
    assert del_resp.headers.get("mcp-protocol-version") == expected

    get_resp = resource_app.client.get("/mcp", headers={**base_headers, "Accept": "application/json"})
    assert get_resp.status_code == 406
    assert get_resp.headers.get("mcp-protocol-version") == expected

    modern_get = resource_app.client.get(
        "/mcp",
        headers={
            "Authorization": "Bearer " + RAW_TOKEN,
            "MCP-Protocol-Version": "2026-07-28",
            "Accept": "application/json",
        },
    )
    assert modern_get.status_code == 405


def test_ask_metric_metrics_and_order_are_accepted_and_audited(resource_app, monkeypatch):
    from avelis.mcp.tools import ToolService

    seen = []
    monkeypatch.setattr(ToolService, "ask_metric", lambda self, **kwargs: seen.append(kwargs) or {"status": "answered"})
    seed_token(resource_app, scope="ask:execute")
    response = call(
        resource_app,
        "ask_metric",
        {
            "metrics": ["metric_a", "metric_d"],
            "start_date": "2026-01-01",
            "end_date": "2026-01-31",
            "dimensions": ["dim_a"],
            "order": "desc",
            "limit": 10,
        },
    )
    assert response.status_code == 200, response.text
    assert len(seen) == 1
    assert seen[0]["metrics"] == ["metric_a", "metric_d"]
    assert seen[0]["start_date"] == datetime(2026, 1, 1).date()
    assert seen[0]["end_date"] == datetime(2026, 1, 31).date()
    assert seen[0]["dimensions"] == ["dim_a"]
    assert seen[0]["order"] == "desc"
    assert seen[0]["limit"] == 10
    with resource_app.maker() as session:
        event = session.scalar(select(AuditEvent))
        summary = event.detail["parameters"]
        assert "metrics" in summary["argument_names"]
        assert "order" in summary["argument_names"]
        assert summary["unknown_argument_count"] == 0


async def test_sdk_legacy_client_session_initialize_handshake(resource_app, monkeypatch):
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from avelis.mcp.server import create_mcp_app
    from avelis.mcp.tools import ToolService

    seed_token(resource_app)
    child = create_mcp_app(resource_app.settings, session_maker=resource_app.maker)
    monkeypatch.setattr(ToolService, "search_metrics", lambda self, **kwargs: [])
    async with child.router.lifespan_context(child):
        app = FastAPI()
        app.mount("/", child)
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport,
            base_url=BASE,
            headers={"Authorization": "Bearer " + RAW_TOKEN, "Origin": BASE},
        ) as client:
            async with streamable_http_client(RESOURCE, http_client=client) as streams:
                async with ClientSession(*streams) as session:
                    init_result = await session.initialize()
                    assert init_result.protocol_version == "2025-11-25"
                    tools = await session.list_tools()
                    assert {tool.name for tool in tools.tools} == set(TOOL_SCOPES)
                    result = await session.call_tool("search_metrics", {})
                    assert not result.is_error


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


def test_whitespace_padded_protocol_headers_are_normalized_before_sdk(resource_app):
    seed_token(resource_app)
    legacy_resp = resource_app.client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + RAW_TOKEN,
            "MCP-Protocol-Version": " 2025-11-25 ",
        },
    )
    assert legacy_resp.status_code == 200, legacy_resp.text
    assert legacy_resp.headers.get("mcp-protocol-version") == "2025-11-25"
    assert {tool["name"] for tool in legacy_resp.json()["result"]["tools"]} == set(TOOL_SCOPES)

    get_resp = resource_app.client.get(
        "/mcp",
        headers={
            "Authorization": "Bearer " + RAW_TOKEN,
            "MCP-Protocol-Version": " 2025-11-25 ",
            "Accept": "application/json",
        },
    )
    assert get_resp.status_code == 406
    assert get_resp.headers.get("mcp-protocol-version") == "2025-11-25"

    modern_resp = resource_app.client.post(
        "/mcp",
        json=modern_message(),
        headers={
            **request_headers(),
            "MCP-Protocol-Version": " 2026-07-28 ",
        },
    )
    assert modern_resp.status_code == 200, modern_resp.text
    assert modern_resp.headers.get("mcp-protocol-version") == "2026-07-28"


@pytest.mark.parametrize(
    "header_pv,requested,expected",
    [
        ("2025-03-26", "2025-11-25", "2025-11-25"),
        ("2025-11-25", "2025-03-26", "2025-03-26"),
        ("2025-03-26", "2026-07-28", "2025-11-25"),
        ("2025-03-26", "2024-11-05", None),
    ],
)
def test_initialize_with_explicit_header_matches_negotiated_body(resource_app, header_pv, requested, expected):
    seed_token(resource_app)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": requested,
            "capabilities": {},
            "clientInfo": {"name": "legacy-client", "version": "1.0"},
        },
    }
    response = resource_app.client.post(
        "/mcp",
        json=payload,
        headers={
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + RAW_TOKEN,
            "MCP-Protocol-Version": header_pv,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["result"]["protocolVersion"] == (expected or "2024-11-05")
    assert response.headers.get("mcp-protocol-version") == expected
