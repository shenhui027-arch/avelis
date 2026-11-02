"""固定 SDK 通过真实 TCP、数据库和现有登录完成 CIMD/DCR 授权。"""

from __future__ import annotations

import json
import ssl
import time
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from fastapi import FastAPI, Request
from mcp import ClientSession
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from mcp_types import PROTOCOL_VERSION_META_KEY
from sqlalchemy import JSON, MetaData, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from tests.unit.mcp_helpers import make_local_tls, tcp_server


@dataclass
class MemoryTokenStorage:
    """客户端持有原始凭据；服务端数据库只保存摘要。"""

    tokens: OAuthToken | None = None
    client_info: OAuthClientInformationFull | None = None
    token_updates: int = 0

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens.model_copy(deep=True)
        self.token_updates += 1

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client_info = client_info.model_copy(deep=True)


@dataclass
class WireEvidence:
    requests: list[dict] = field(default_factory=list)
    responses: list[dict] = field(default_factory=list)

    async def request(self, request: httpx2.Request) -> None:
        body = await request.aread()
        headers = {
            key: "<redacted>" if key.lower() == "authorization" else value for key, value in request.headers.items()
        }
        item = {"method": request.method, "path": request.url.path, "headers": headers}
        # wire 证据只留 grant/resource，不保留令牌请求或响应正文。
        if request.url.path == "/oauth/token":
            form = parse_qs(body.decode())
            item["grant_type"] = form.get("grant_type", [None])[0]
            item["resource"] = form.get("resource", [None])[0]
        elif request.url.path == "/mcp" and body:
            item["rpc"] = json.loads(body)
        self.requests.append(item)

    async def response(self, response: httpx2.Response) -> None:
        self.responses.append(
            {
                "path": response.request.url.path,
                "status": response.status_code,
                "headers": dict(response.headers),
            }
        )


@pytest.fixture
def sdk_database(monkeypatch, tmp_path):
    monkeypatch.setenv("AVELIS_SECRET", "test-secret-mcp-sdk-e2e")
    monkeypatch.setenv("AVELIS_ENV_FILE", str(tmp_path / "missing.env"))
    from avelis import cache, config, db, security
    from avelis.api import app as app_module
    from avelis.api import auth, deps
    from avelis.models import Domain, DomainMember, User, UserRole

    engine = create_engine(
        f"sqlite:///{(tmp_path / 'mcp-sdk.sqlite').as_posix()}", connect_args={"check_same_thread": False}
    )
    metadata = MetaData()
    for table in db.Base.metadata.tables.values():
        copied = table.to_metadata(metadata)
        for column in copied.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "session_factory", lambda: factory)
    monkeypatch.setattr(deps, "session_factory", lambda: factory)
    # 本测试验证真实 JWT/current_user，登录限流与登出缓存另有定向测试。
    monkeypatch.setattr(cache, "is_revoked", lambda _: False)
    monkeypatch.setattr(cache, "login_locked_seconds", lambda *_: 0)
    monkeypatch.setattr(cache, "clear_login_failures", lambda _: None)
    settings = config.build_settings({"AVELIS_SECRET": "test-secret-mcp-sdk-e2e"})
    state = SimpleNamespace(factory=factory, settings=settings, user_id="sdk-user", domain_id="sdk-domain")
    for module in (config, db, security, app_module, auth):
        monkeypatch.setattr(module, "get_settings", lambda: state.settings)
    with factory.begin() as session:
        session.add(
            User(
                id=state.user_id,
                username="sdk-user",
                display_name="SDK 测试用户",
                password_hash=security.hash_password("SDK-fixture-password-2026"),
            )
        )
        session.add(UserRole(user_id=state.user_id, role="analyst"))
        session.add(Domain(id=state.domain_id, code="sdk", name="SDK 测试域", owner_id=state.user_id))
        session.add(DomainMember(domain_id=state.domain_id, user_id=state.user_id, role="viewer"))
    try:
        yield state
    finally:
        engine.dispose()


def database_token(factory, raw):
    from avelis.models import OAuthToken
    from avelis.oauth.repository import credential_hash

    with factory() as session:
        return session.scalar(select(OAuthToken).where(OAuthToken.access_token_hash == credential_hash(raw)))


@pytest.mark.parametrize("registration", ["cimd", "dcr"])
async def test_real_sdk_oauth_discover_call_refresh_revoke(registration, sdk_database, monkeypatch, tmp_path):
    from avelis.api.app import create_app
    from avelis.models import AuditEvent, OAuthAuthorizationCode, OAuthClient, OAuthConsentTicket, OAuthToken
    from avelis.oauth import router as oauth_router
    from avelis.oauth.repository import credential_hash

    certificate = make_local_tls(tmp_path)
    tls_context = ssl.create_default_context(cafile=str(certificate[0]))
    callbacks: list[AuthorizationCodeResult] = []
    authorization_urls: list[str] = []
    tickets: list[str] = []
    metadata_fetches: list[str] = []
    requested_scope = "metrics:read lineage:read ask:execute"
    original_fetch = oauth_router.fetch_client_metadata

    async def trusted_local_cimd(client_id, **kwargs):
        # 只增加测试 CA，仍走正式拉取器的 DNS/IP 固定、限制和证书验证。
        return await original_fetch(client_id, ca_file=str(certificate[0]), **kwargs)

    monkeypatch.setattr(oauth_router, "fetch_client_metadata", trusted_local_cimd)

    def fixture_factory(fixture_url):
        fixture = FastAPI()

        @fixture.get("/client.json")
        async def client_document():
            metadata_fetches.append(fixture_url + "/client.json")
            return {
                "client_id": fixture_url + "/client.json",
                "client_name": "Actual MCP SDK CIMD fixture",
                "redirect_uris": [fixture_url + "/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "scope": requested_scope,
            }

        @fixture.get("/callback")
        async def callback(request: Request):
            callbacks.append(AuthorizationCodeResult.model_validate(dict(request.query_params)))
            return {"authorization_callback_received": True}

        return fixture

    with tcp_server(fixture_factory, tls=certificate) as (fixture_url, _):
        cimd_url = fixture_url + "/client.json"

        def host_factory(base_url):
            sdk_database.settings = replace(
                sdk_database.settings,
                public_url=base_url,
                mcp_enabled=True,
                mcp_cimd_allowed_urls=(cimd_url,),
                frontend_dir=None,
            )
            return create_app()

        with tcp_server(host_factory) as (base_url, _):
            resource = base_url + "/mcp"
            storage = MemoryTokenStorage()
            wire = WireEvidence()
            async with httpx2.AsyncClient(trust_env=False) as login_client:
                login = await login_client.post(
                    base_url + "/api/auth/token",
                    data={"username": "sdk-user", "password": "SDK-fixture-password-2026"},
                )
                assert login.status_code == 200, login.text
                bearer = {"Authorization": "Bearer " + login.json()["access_token"]}

            async def redirect_handler(url):
                authorization_urls.append(url)
                authorization = parse_qs(urlsplit(url).query)
                assert authorization["code_challenge_method"] == ["S256"]
                assert authorization["resource"] == [resource]
                assert authorization["redirect_uri"] == [fixture_url + "/callback"]
                async with httpx2.AsyncClient(verify=tls_context, trust_env=False) as browser:
                    authorization_response = await browser.get(url)
                    assert authorization_response.status_code == 302, authorization_response.text
                    consent_url = urlsplit(authorization_response.headers["location"])
                    assert consent_url.path == "/oauth/consent"
                    ticket = parse_qs(consent_url.query)["ticket"][0]
                    tickets.append(ticket)
                    consent_path = base_url + "/api/oauth/consent/" + ticket
                    assert (await browser.get(consent_path)).status_code == 401
                    assert (await browser.post(consent_path + "/approve")).status_code == 401
                    consent = await browser.get(consent_path, headers=bearer)
                    assert consent.status_code == 200, consent.text
                    view = consent.json()
                    assert view["client_id"] == authorization["client_id"][0]
                    assert {item["scope"] for item in view["scopes"]} == set(requested_scope.split())
                    assert view["source"] == ("cimd" if registration == "cimd" else "dynamic")
                    assert [domain["id"] for domain in view["domains"]] == [sdk_database.domain_id]
                    approved = await browser.post(consent_path + "/approve", headers=bearer)
                    assert approved.status_code == 200, approved.text
                    response = await browser.get(approved.json()["redirect_to"])
                    response.raise_for_status()
                    assert str(response.url).startswith(fixture_url + "/callback?")

            async def callback_handler():
                assert callbacks, "The local callback must actually receive the approved redirect"
                return callbacks[-1]

            provider = OAuthClientProvider(
                server_url=resource,
                client_metadata=OAuthClientMetadata(
                    client_name="Actual MCP SDK Avelis fixture",
                    redirect_uris=[fixture_url + "/callback"],
                    grant_types=["authorization_code", "refresh_token"],
                    response_types=["code"],
                    token_endpoint_auth_method="none",
                    scope=requested_scope,
                ),
                storage=storage,
                redirect_handler=redirect_handler,
                callback_handler=callback_handler,
                client_metadata_url=cimd_url if registration == "cimd" else None,
            )

            async with httpx2.AsyncClient(
                auth=provider,
                verify=tls_context,
                trust_env=False,
                timeout=10,
                event_hooks={"request": [wire.request], "response": [wire.response]},
            ) as client:
                async with streamable_http_client(resource, http_client=client) as streams:
                    async with ClientSession(*streams) as session:
                        discovery = await session.discover()
                        assert "2026-07-28" in discovery.supported_versions
                        assert session.initialize_result is None
                        tools = await session.list_tools()
                        assert {tool.name for tool in tools.tools} == {
                            "search_metrics",
                            "get_metric",
                            "ask_metric",
                            "lineage_neighbors",
                            "list_changes",
                        }
                        result = await session.call_tool("search_metrics", {})
                        assert not result.is_error
                        assert all(not item.text.strip() or json.loads(item.text) == [] for item in result.content)
                        assert storage.tokens is not None and storage.client_info is not None
                        assert storage.tokens.expires_in == 900
                        original_access = storage.tokens.access_token
                        original_refresh = storage.tokens.refresh_token
                        assert original_refresh
                        record = database_token(sdk_database.factory, original_access)
                        assert record is not None and record.resource == resource
                        assert record.user_id == sdk_database.user_id
                        assert record.refresh_token_hash == credential_hash(original_refresh)

                        # 使用 SDK 自身的到期状态触发实际 refresh，不替换 SDK 授权流程。
                        provider.context.token_expiry_time = time.time() - 1
                        refreshed_result = await session.call_tool("search_metrics", {})
                        assert not refreshed_result.is_error
                        assert storage.token_updates == 2
                        refreshed_access = storage.tokens.access_token
                        assert refreshed_access != original_access
                        refreshed_record = database_token(sdk_database.factory, refreshed_access)
                        assert refreshed_record is not None and refreshed_record.resource == resource
                        assert refreshed_record.family_id == record.family_id
                        assert refreshed_record.parent_token_id == record.id
                        assert database_token(sdk_database.factory, original_access).access_revoked_at is not None
                        refreshes = [r for r in wire.requests if r.get("grant_type") == "refresh_token"]
                        assert len(refreshes) == 1 and refreshes[0]["resource"] == resource

                        async with httpx2.AsyncClient(trust_env=False) as bare:
                            response = await bare.post(
                                base_url + "/oauth/revoke",
                                data={
                                    "token": refreshed_access,
                                    "token_type_hint": "access_token",
                                    "client_id": storage.client_info.client_id,
                                },
                            )
                            assert response.status_code == 200
                            rejected = await bare.post(
                                resource,
                                headers={
                                    "Authorization": "Bearer " + refreshed_access,
                                    "Mcp-Protocol-Version": "2026-07-28",
                                    "Accept": "application/json, text/event-stream",
                                },
                                json={
                                    "jsonrpc": "2.0",
                                    "id": 900,
                                    "method": "tools/call",
                                    "params": {
                                        "name": "search_metrics",
                                        "arguments": {},
                                        "_meta": {PROTOCOL_VERSION_META_KEY: "2026-07-28"},
                                    },
                                },
                            )
                            assert rejected.status_code == 401
                            assert "oauth-protected-resource/mcp" in rejected.headers["www-authenticate"]

                        # 吊销后 SDK 会重新授权；首个 401 与新凭据分别断言。
                        response_start = len(wire.responses)
                        reauthorized = await session.call_tool("search_metrics", {})
                        assert not reauthorized.is_error
                        after_revoke = wire.responses[response_start:]
                        assert next(r for r in after_revoke if r["path"] == "/mcp")["status"] == 401
                        assert len(callbacks) == 2
                        assert storage.tokens.access_token != refreshed_access

            assert all(callback.iss == base_url for callback in callbacks)
            assert len(authorization_urls) == len(tickets) == 2
            assert any(r["path"] == "/.well-known/oauth-protected-resource/mcp" for r in wire.requests)
            assert any(r["path"] == "/.well-known/oauth-authorization-server" for r in wire.requests)
            registrations = [r for r in wire.requests if r["path"] == "/oauth/register"]
            if registration == "cimd":
                assert not registrations and metadata_fetches
                assert storage.client_info.client_id == cimd_url
            else:
                assert len(registrations) == 1 and not metadata_fetches
                assert storage.client_info.client_id != cimd_url

            exchanges = [r for r in wire.requests if r.get("grant_type") == "authorization_code"]
            assert len(exchanges) == 2 and all(r["resource"] == resource for r in exchanges)
            mcp_requests = [r for r in wire.requests if r["path"] == "/mcp"]
            mcp_responses = [r for r in wire.responses if r["path"] == "/mcp"]
            assert mcp_requests and mcp_responses[0]["status"] == 401
            assert "oauth-protected-resource/mcp" in mcp_responses[0]["headers"]["www-authenticate"]
            assert all("mcp-session-id" not in r["headers"] for r in mcp_requests + mcp_responses)
            rpc_requests = [r for r in mcp_requests if "rpc" in r]
            methods = [r["rpc"]["method"] for r in rpc_requests]
            assert {"server/discover", "tools/list", "tools/call"} <= set(methods)
            assert "initialize" not in methods and "notifications/initialized" not in methods
            assert all(r["headers"].get("mcp-protocol-version") == "2026-07-28" for r in rpc_requests)
            assert all(r["rpc"]["params"]["_meta"][PROTOCOL_VERSION_META_KEY] == "2026-07-28" for r in rpc_requests)
            with sdk_database.factory() as db_session:
                stored = [
                    vars(row)
                    for model in (OAuthClient, OAuthConsentTicket, OAuthAuthorizationCode, OAuthToken)
                    for row in db_session.scalars(select(model))
                ]
                persisted = repr(stored)
                for raw in [
                    original_access,
                    original_refresh,
                    refreshed_access,
                    *tickets,
                    *[c.code for c in callbacks],
                ]:
                    assert raw not in persisted
                events = list(db_session.scalars(select(AuditEvent).where(AuditEvent.action == "mcp.search_metrics")))
                assert len(events) == 3
                assert all(event.actor_id == sdk_database.user_id for event in events)
                assert all(event.detail["client_id"] == storage.client_info.client_id for event in events)
                assert all(event.detail.get("token_id") for event in events)
                audit_details = repr([event.detail for event in events])
                assert original_access not in audit_details and original_refresh not in audit_details
                assert "rows" not in audit_details
