"""M1A 认证安全底座与标准扩展（OIDC/ForwardAuth/TOTP/PAT/Admin Ops）综合单元测试。"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from datetime import timedelta
from math import ceil
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from redis.exceptions import RedisError
from sqlalchemy import JSON, MetaData, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from avelis import auth_config, cache, security, totp
from avelis.api import app as app_module
from avelis.api import auth, deps
from avelis.config import build_settings
from avelis.db import Base, utcnow
from avelis.models import (
    AuditEvent,
    Datasource,
    Domain,
    DomainMember,
    Job,
    PersonalAccessToken,
    User,
    UserRole,
)


class _MemoryPipeline:
    def __init__(self, store):
        self.store = store
        self.commands = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def get(self, name):
        self.commands.append(("get", name))
        return self

    def ttl(self, name):
        self.commands.append(("ttl", name))
        return self

    def incr(self, name):
        self.commands.append(("incr", name))
        return self

    def expire(self, name, seconds, nx=False):
        self.commands.append(("expire", name, seconds, nx))
        return self

    def execute(self):
        if self.store.fail:
            raise RedisError("redis offline")
        out = []
        for cmd in self.commands:
            op = cmd[0]
            if op == "get":
                out.append(self.store.get(cmd[1]))
            elif op == "ttl":
                name = cmd[1]
                out.append(self.store.ttl(name) if name in self.store.deadlines else -2)
            elif op == "incr":
                out.append(self.store.incr(cmd[1]))
            elif op == "expire":
                _, name, seconds, nx = cmd
                if not nx or name not in self.store.deadlines:
                    self.store.expire(name, seconds)
                out.append(True)
        return out


class _MemoryRedis:
    def __init__(self):
        self.values = {}
        self.deadlines = {}
        self.fail = False

    def pipeline(self, transaction=True):
        assert transaction is True
        return _MemoryPipeline(self)

    def get(self, name):
        if self.fail:
            raise RedisError("redis offline")
        if self.deadlines.get(name, float("inf")) <= time.time():
            self.delete(name)
        return self.values.get(name)

    def set(self, name, value, ex, nx=False):
        if self.fail:
            raise RedisError("redis offline")
        if nx and self.get(name) is not None:
            return False
        self.values[name] = value
        self.expire(name, ex)
        return True

    def exists(self, name):
        if self.fail:
            raise RedisError("redis offline")
        return int(self.get(name) is not None)

    def incr(self, name):
        if self.fail:
            raise RedisError("redis offline")
        self.values[name] = int(self.get(name) or 0) + 1
        return self.values[name]

    def expire(self, name, seconds):
        self.deadlines[name] = time.time() + seconds
        return True

    def ttl(self, name):
        return ceil(self.deadlines[name] - time.time())

    def delete(self, name):
        if self.fail:
            raise RedisError("redis offline")
        existed = int(name in self.values)
        self.values.pop(name, None)
        self.deadlines.pop(name, None)
        return existed


@pytest.fixture
def env(monkeypatch, tmp_path):
    frontend_dist = tmp_path / "dist"
    frontend_dist.mkdir()
    (frontend_dist / "index.html").write_text("<html>SPA</html>", encoding="utf-8")

    settings = build_settings(
        {
            "AVELIS_SECRET": "offline-unit-test-secret-0123456789",
            "AVELIS_ENV_FILE": "/nonexistent",
            "AVELIS_FRONTEND_DIR": str(frontend_dist),
            "AVELIS_ENVIRONMENT": "production",
        }
    )
    store = _MemoryRedis()
    for mod in (security, auth, app_module):
        monkeypatch.setattr(mod, "get_settings", lambda: settings)
    monkeypatch.setattr(cache, "client", lambda: store)
    cache.clear_local_fallbacks()
    auth_config._invalidate_discovery_cache()

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for table in Base.metadata.tables.values():
        copied = table.to_metadata(metadata)
        for col in copied.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()
    metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)

    with factory.begin() as session:
        admin = User(
            id="u-admin",
            username="admin",
            display_name="管理员",
            email="admin@example.com",
            password_hash=security.hash_password("Admin-Pass-2026!"),
            must_change_password=False,
        )
        alice = User(
            id="u-alice",
            username="alice",
            display_name="Alice Wang",
            email="alice@example.com",
            password_hash=security.hash_password("Alice-Pass-2026!"),
            must_change_password=False,
        )
        bob = User(
            id="u-bob",
            username="bob",
            display_name="Bob Chen",
            email="bob@example.com",
            password_hash=security.hash_password("Bob-Pass-2026!"),
            must_change_password=True,
        )
        session.add_all(
            [
                admin,
                alice,
                bob,
                UserRole(user_id=admin.id, role="admin"),
                UserRole(user_id=alice.id, role="steward"),
                UserRole(user_id=bob.id, role="analyst"),
                Domain(id="dom-1", code="sales_dom", name="域一", owner_id=alice.id),
                DomainMember(domain_id="dom-1", user_id=alice.id, role="owner"),
                DomainMember(domain_id="dom-1", user_id=bob.id, role="viewer"),
                Datasource(id="ds-1", name="源一", kind="postgres", host="localhost", port=5432, options={}),
                Job(id="job-alice", kind="collect", status="queued", payload={"ds": "ds-1"}, created_by=alice.id),
                Job(id="job-sys", kind="collect", status="queued", payload={"ds": "ds-1"}, created_by=None),
            ]
        )

    app = app_module.create_app(settings=settings)

    def override_session():
        with factory() as s:
            try:
                yield s
                s.commit()
            except Exception:
                s.rollback()
                raise

    app.dependency_overrides[deps.get_session] = override_session
    monkeypatch.setattr(deps, "session_factory", lambda: factory)

    try:
        with TestClient(app) as client:
            yield SimpleNamespace(
                app=app,
                client=client,
                factory=factory,
                store=store,
                settings=settings,
                set_settings=lambda s: _swap_settings(monkeypatch, s),
            )
    finally:
        cache.clear_local_fallbacks()
        engine.dispose()


def _swap_settings(monkeypatch, new_settings):
    for mod in (security, auth, app_module):
        monkeypatch.setattr(mod, "get_settings", lambda: new_settings)


def _login(client: TestClient, username: str, password: str, **extra) -> httpx.Response:
    return client.post("/api/auth/token", data={"username": username, "password": password, **extra})


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_must_change_password_blocks_api_oauth_consent_and_mcp(env):
    r = _login(env.client, "bob", "Bob-Pass-2026!")
    assert r.status_code == 200 and r.json()["must_change_password"] is True
    token = r.json()["access_token"]

    # 非放行接口全部 403
    for method, path in (
        ("GET", "/api/datasources"),
        ("POST", "/api/auth/refresh"),
        ("GET", "/api/me/totp"),
        ("GET", "/api/me/tokens"),
        ("GET", "/api/oauth/consent/some-ticket"),
        ("POST", "/api/oauth/consent/some-ticket/approve"),
        ("POST", "/api/oauth/consent/some-ticket/deny"),
    ):
        resp = env.client.request(method, path, headers=_bearer(token))
        assert resp.status_code == 403, (method, path, resp.text)
        assert resp.json()["detail"]["code"] == "must_change_password"

    # /oauth/consent SPA 页面：未登录返回 200，已登录且 must_change_password=True 返回 403（Cookie 与 Bearer 均拦截）
    assert env.client.get("/oauth/consent?ticket=example", headers=_bearer(token)).status_code == 403
    assert env.client.get("/oauth/consent?ticket=example").status_code == 403  # Cookie 仍属于 bob
    env.client.cookies.clear()
    assert env.client.get("/oauth/consent?ticket=example").status_code == 200

    # 放行列表允许 /api/me、/api/me/password、/api/auth/logout
    assert env.client.get("/api/me", headers=_bearer(token)).status_code == 200
    assert env.client.post("/api/auth/logout", headers=_bearer(token)).status_code == 200

    # MCP 工具执行拒绝 must_change_password=True
    from mcp.server.auth.middleware import auth_context
    from mcp.server.mcpserver.exceptions import ToolError

    from avelis.mcp import server as mcp_server

    mcp_app = mcp_server.create_mcp_app(env.settings, session_maker=lambda: env.factory())
    tool = mcp_app.state.mcp_server._tool_manager.get_tool("search_metrics")
    monkeypatch_token = SimpleNamespace(subject="u-bob", scopes=["metrics:read"], client_id="c1")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mcp_server, "get_access_token", lambda: monkeypatch_token)
        mp.setattr(mcp_server, "audit_tool_call", lambda *_: None)
        tok = auth_context.auth_context_var.set(SimpleNamespace())
        try:
            with pytest.raises(ToolError, match="首次登录或密码重置后必须修改密码"):
                tool.fn()
        finally:
            auth_context.auth_context_var.reset(tok)


def test_job_idor_and_domain_member_delete(env):
    admin_tok = _login(env.client, "admin", "Admin-Pass-2026!").json()["access_token"]
    env.client.cookies.clear()
    alice_tok = _login(env.client, "alice", "Alice-Pass-2026!").json()["access_token"]
    env.client.cookies.clear()

    with env.factory.begin() as s:
        s.get(User, "u-bob").must_change_password = False
    bob_tok = _login(env.client, "bob", "Bob-Pass-2026!").json()["access_token"]
    env.client.cookies.clear()

    # Job IDOR
    assert env.client.get("/api/jobs/job-alice", headers=_bearer(alice_tok)).status_code == 200
    assert env.client.get("/api/jobs/job-alice", headers=_bearer(admin_tok)).status_code == 200
    assert env.client.get("/api/jobs/job-sys", headers=_bearer(admin_tok)).status_code == 200
    assert env.client.get("/api/jobs/job-alice", headers=_bearer(bob_tok)).status_code == 403
    assert env.client.get("/api/jobs/job-sys", headers=_bearer(alice_tok)).status_code == 403

    # Domain member delete
    assert env.client.delete("/api/domains/dom-1/members/u-bob", headers=_bearer(bob_tok)).status_code == 403
    assert env.client.delete("/api/domains/dom-1/members/u-alice", headers=_bearer(alice_tok)).status_code == 409
    assert env.client.delete("/api/domains/dom-1/members/missing", headers=_bearer(alice_tok)).status_code == 404
    removed = env.client.delete("/api/domains/dom-1/members/u-bob", headers=_bearer(alice_tok))
    assert removed.status_code == 200
    assert all(m["user_id"] != "u-bob" for m in removed.json()["members"])
    with env.factory() as s:
        actions = [e.action for e in s.scalars(select(AuditEvent).where(AuditEvent.object_id == "dom-1"))]
        assert "domain.member_remove" in actions


def test_security_headers_docs_toggle_and_cors(env):
    # 默认 production 关闭 /api/docs 与 /api/openapi.json
    r = env.client.get("/api/health")
    assert r.status_code == 200
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert r.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"
    assert "script-src 'self';" in r.headers["content-security-policy"]
    assert "strict-transport-security" not in r.headers
    assert env.client.get("/api/docs").status_code == 404
    assert env.client.get("/api/openapi.json").status_code == 404

    # 启用 docs 与 https public_url
    dev_settings = build_settings(
        {
            "AVELIS_SECRET": "offline-unit-test-secret-0123456789",
            "AVELIS_ENVIRONMENT": "development",
            "AVELIS_PUBLIC_URL": "https://avelis.example",
        }
    )
    assert dev_settings.docs_enabled is True
    dev_app = app_module.create_app(settings=dev_settings)
    with TestClient(dev_app) as dev_client:
        docs = dev_client.get("/api/docs")
        assert docs.status_code == 200
        assert "https://cdn.jsdelivr.net" in docs.headers["content-security-policy"]
        health = dev_client.get("/api/health", headers={"Origin": "https://avelis.example"})
        assert health.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"
        assert health.headers["access-control-allow-origin"] == "https://avelis.example"
        assert health.headers["access-control-allow-credentials"] == "true"

    # 通配符 CORS 拒绝
    with pytest.raises(ValueError, match="AVELIS_CORS_ORIGINS"):
        build_settings({"AVELIS_SECRET": "offline-unit-test-secret-0123456789", "AVELIS_CORS_ORIGINS": "*"})


def test_bruteforce_dual_lockout_constant_time_normalization_and_redis_resilience(env):
    # 大小写与前后空白归一化
    ok = _login(env.client, "  AlIcE ", "Alice-Pass-2026!")
    assert ok.status_code == 200
    env.client.cookies.clear()

    # 恒定耗时函数遇到非 Argon2 哈希不抛 UnknownHashError
    assert security.verify_password("anything", "!") is False
    assert security.verify_password("anything", "unused") is False
    assert security.verify_password("anything", "") is False

    # 账号维度锁定（5 次）并返回结构化 retry_after；同 IP 其他账号仍返回 401
    for _ in range(5):
        assert _login(env.client, "ALICE", "wrong-pass").status_code == 401
    locked = _login(env.client, "alice", "Alice-Pass-2026!")
    assert locked.status_code == 429
    assert int(locked.headers["Retry-After"]) > 0
    assert locked.json()["detail"]["code"] == "login_locked"
    assert locked.json()["detail"]["retry_after"] > 0
    assert _login(env.client, "nonexistent_user", "wrong").status_code == 401

    # IP 维度锁定（默认 20 次，已失败 6 次，再失败 14 次触发 IP 锁）
    for idx in range(14):
        assert _login(env.client, f"ghost_{idx}", "wrong").status_code == 401
    ip_locked = _login(env.client, "admin", "Admin-Pass-2026!")
    assert ip_locked.status_code == 429
    assert ip_locked.json()["detail"]["code"] == "login_locked"

    # Redis 故障时鉴权路径不 500
    env.store.fail = True
    resilient = _login(env.client, "admin", "Admin-Pass-2026!")
    assert resilient.status_code == 200
    tok = resilient.json()["access_token"]
    assert env.client.get("/api/me", headers=_bearer(tok)).status_code == 200
    assert env.client.post("/api/auth/refresh", headers=_bearer(tok)).status_code == 200
    assert env.client.post("/api/auth/logout", headers=_bearer(tok)).status_code == 200
    env.store.fail = False


def test_password_change_rate_limit_and_cookie_remember_me(env):
    r = _login(env.client, "alice", "Alice-Pass-2026!", remember_me="true")
    assert r.status_code == 200
    assert "avelis_access" in env.client.cookies
    assert "avelis_refresh" in r.headers.get("set-cookie", "") or True
    set_cookies = r.headers.get_list("set-cookie")
    assert any("avelis_access=" in c and "HttpOnly" in c and "Max-Age=" in c for c in set_cookies)
    assert any("avelis_refresh=" in c and "HttpOnly" in c and "Max-Age=" in c for c in set_cookies)

    # Cookie 认证免 Bearer 访问 /api/me 与 /api/auth/refresh
    assert env.client.get("/api/me").status_code == 200
    refreshed = env.client.post("/api/auth/refresh")
    assert refreshed.status_code == 200

    # 改密码限流
    for _ in range(5):
        wrong = env.client.post(
            "/api/me/password",
            json={"current_password": "wrong-password", "new_password": "New-Pass-2026!"},
        )
        assert wrong.status_code == 403
    limited = env.client.post(
        "/api/me/password",
        json={"current_password": "wrong-password", "new_password": "New-Pass-2026!"},
    )
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) > 0

    # 退出清理 Cookie
    out = env.client.post("/api/auth/logout")
    assert out.status_code == 200
    assert env.client.get("/api/me").status_code == 401


def test_auth_settings_branding_xss_guard_and_lockout_prevention(env):
    admin_tok = _login(env.client, "admin", "Admin-Pass-2026!").json()["access_token"]
    env.client.cookies.clear()
    alice_tok = _login(env.client, "alice", "Alice-Pass-2026!").json()["access_token"]
    env.client.cookies.clear()

    # 公开端点无需登录
    pub = env.client.get("/api/auth/config")
    assert pub.status_code == 200
    assert pub.json()["local_login_enabled"] is True
    assert pub.json()["oidc"] is None
    assert pub.json()["branding"]["brand_name"] == "Avelis"

    # 非管理员禁止读取/修改系统认证设置
    assert env.client.get("/api/settings/auth", headers=_bearer(alice_tok)).status_code == 403

    base_payload = {
        "local_login_enabled": True,
        "forward_auth_enabled": False,
        "forward_auth_header": "X-Forwarded-User",
        "forward_auth_auto_create": True,
        "oidc": {
            "enabled": True,
            "provider_name": "Okta",
            "issuer_url": "https://idp.example.com",
            "client_id": "avelis-web",
            "client_secret": "super-secret-value-1234",
            "scopes": "profile email",
            "auto_create_user": True,
            "default_role": "analyst",
        },
        "branding": {
            "brand_name": "Acme BI",
            "brand_logo_url": "https://cdn.example.com/logo.png",
            "login_subtitle": "企业指标平台",
            "terms_url": "/legal/terms",
            "privacy_url": "https://cdn.example.com/privacy",
        },
    }
    saved = env.client.put("/api/settings/auth", headers=_bearer(admin_tok), json=base_payload)
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["oidc"]["has_client_secret"] is True
    assert body["oidc"]["client_secret_hint"] == "••••••••1234"
    assert "super-secret-value-1234" not in saved.text
    assert body["oidc"]["scopes"] == "openid profile email"

    # 公开端点只暴露必要非敏感信息
    pub2 = env.client.get("/api/auth/config").json()
    assert pub2["oidc"] == {"enabled": True, "provider_name": "Okta", "authorize_url": "/api/auth/oidc/login"}
    assert pub2["branding"]["brand_name"] == "Acme BI"

    # 拒绝 XSS URL
    bad_xss = json.loads(json.dumps(base_payload))
    bad_xss["branding"]["terms_url"] = "javascript:alert(1)"
    assert env.client.put("/api/settings/auth", headers=_bearer(admin_tok), json=bad_xss).status_code == 422

    # 拒绝同时禁用所有登录方式
    lockout = json.loads(json.dumps(base_payload))
    lockout["local_login_enabled"] = False
    lockout["oidc"]["enabled"] = False
    assert env.client.put("/api/settings/auth", headers=_bearer(admin_tok), json=lockout).status_code == 422


def test_oidc_pkce_jit_flow_and_replay_protection(env, monkeypatch):
    admin_tok = _login(env.client, "admin", "Admin-Pass-2026!").json()["access_token"]
    env.client.cookies.clear()

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk["kid"] = "k1"
    public_jwk["use"] = "sig"
    public_jwk["alg"] = "RS256"

    issuer = "https://idp.example.com"
    client_id = "avelis-client"

    assert (
        env.client.put(
            "/api/settings/auth",
            headers=_bearer(admin_tok),
            json={
                "local_login_enabled": True,
                "forward_auth_enabled": False,
                "forward_auth_header": "X-Forwarded-User",
                "forward_auth_auto_create": True,
                "oidc": {
                    "enabled": True,
                    "provider_name": "Corp IdP",
                    "issuer_url": issuer,
                    "client_id": client_id,
                    "client_secret": "oidc-secret-9999",
                    "scopes": "openid profile email",
                    "auto_create_user": True,
                    "default_role": "analyst",
                },
                "branding": {"brand_name": "Avelis"},
            },
        ).status_code
        == 200
    )

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": issuer,
                    "authorization_endpoint": f"{issuer}/authorize",
                    "token_endpoint": f"{issuer}/token",
                    "jwks_uri": f"{issuer}/jwks",
                },
            )
        if request.url.path == "/jwks":
            return httpx.Response(200, json={"keys": [public_jwk]})
        if request.url.path == "/token":
            form = parse_qs(request.content.decode())
            captured["code_verifier"] = form["code_verifier"][0]
            now_ts = int(time.time())
            id_token = jwt.encode(
                {
                    "iss": issuer,
                    "sub": "ext-user-001",
                    "aud": client_id,
                    "iat": now_ts,
                    "exp": now_ts + 300,
                    "nonce": captured["nonce"],
                    "preferred_username": "Jit.User",
                    "name": "JIT 测试用户",
                    "email": "jit@example.com",
                },
                private_key,
                algorithm="RS256",
                headers={"kid": "k1"},
            )
            return httpx.Response(200, json={"access_token": "idp-at", "id_token": id_token, "token_type": "Bearer"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(auth_config, "_http_client", lambda: httpx.AsyncClient(transport=transport))

    login_resp = env.client.get("/api/auth/oidc/login?next=/lineage&remember_me=true", follow_redirects=False)
    assert login_resp.status_code == 302
    auth_loc = urlsplit(login_resp.headers["location"])
    qs = parse_qs(auth_loc.query)
    captured["nonce"] = qs["nonce"][0]
    assert qs["code_challenge_method"] == ["S256"]

    cb = env.client.get(
        f"/api/auth/oidc/callback?code=auth-code-1&state={qs['state'][0]}",
        follow_redirects=False,
    )
    assert cb.status_code == 302
    assert cb.headers["location"] == "/lineage"
    me = env.client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["username"] == "jit.user"
    assert me.json()["auth_provider"] == "oidc"
    assert me.json()["roles"] == ["analyst"]

    # 同一 state 重放被拒绝
    replay = env.client.get(
        f"/api/auth/oidc/callback?code=auth-code-1&state={qs['state'][0]}",
        follow_redirects=False,
    )
    assert replay.status_code == 302 and "error=" in replay.headers["location"]


def test_forward_auth_trusted_proxy_and_secret(env):
    admin_tok = _login(env.client, "admin", "Admin-Pass-2026!").json()["access_token"]
    env.client.cookies.clear()

    assert (
        env.client.put(
            "/api/settings/auth",
            headers=_bearer(admin_tok),
            json={
                "local_login_enabled": True,
                "forward_auth_enabled": True,
                "forward_auth_header": "X-Forwarded-User",
                "forward_auth_auto_create": True,
                "forward_auth_secret": "proxy-shared-secret",
                "oidc": {"enabled": False},
                "branding": {"brand_name": "Avelis"},
            },
        ).status_code
        == 200
    )

    # 缺少或错误共享密钥拒绝
    assert env.client.get("/api/auth/forward", headers={"X-Forwarded-User": "Proxy.User"}).status_code == 401
    ok = env.client.get(
        "/api/auth/forward",
        headers={"X-Forwarded-User": "Proxy.User", "X-Forwarded-Auth-Secret": "proxy-shared-secret"},
    )
    assert ok.status_code == 200
    assert env.client.get("/api/me").json()["username"] == "proxy.user"

    # 关闭共享密钥且 forwarded_allow_ips="*" 时拒绝不安全配置
    assert (
        env.client.put(
            "/api/settings/auth",
            headers=_bearer(admin_tok),
            json={
                "local_login_enabled": True,
                "forward_auth_enabled": True,
                "forward_auth_header": "X-Forwarded-User",
                "forward_auth_auto_create": True,
                "clear_forward_auth_secret": True,
                "oidc": {"enabled": False},
                "branding": {"brand_name": "Avelis"},
            },
        ).status_code
        == 200
    )
    env.set_settings(replace(env.settings, forwarded_allow_ips="*"))
    assert env.client.get("/api/auth/forward", headers={"X-Forwarded-User": "Proxy.User"}).status_code == 403


def test_totp_2fa_lifecycle_and_replay_protection(env):
    alice_tok = _login(env.client, "alice", "Alice-Pass-2026!").json()["access_token"]
    env.client.cookies.clear()

    assert env.client.get("/api/me/totp", headers=_bearer(alice_tok)).json()["enabled"] is False
    enroll = env.client.post("/api/me/totp/enroll", headers=_bearer(alice_tok))
    assert enroll.status_code == 200
    secret = enroll.json()["secret"]
    assert enroll.json()["otpauth_uri"].startswith("otpauth://totp/")

    code = totp.hotp_code(secret, totp.totp_step())
    verified = env.client.post("/api/me/totp/verify", headers=_bearer(alice_tok), json={"code": code})
    assert verified.status_code == 200 and verified.json()["enabled"] is True

    # 登录触发 2FA 挑战
    challenge_resp = _login(env.client, "alice", "Alice-Pass-2026!")
    assert challenge_resp.status_code == 403
    detail = challenge_resp.json()["detail"]
    assert detail["totp_required"] is True
    challenge_token = detail["challenge_token"]

    # 同一时间步重放被拒绝
    replay_step = env.client.post(
        "/api/auth/totp/verify",
        json={"challenge_token": challenge_token, "code": code},
    )
    assert replay_step.status_code == 401

    # 下一时间步通过，并且 challenge_token 不可重用
    next_code = totp.hotp_code(secret, totp.totp_step() + 1)
    done = env.client.post(
        "/api/auth/totp/verify",
        json={"challenge_token": challenge_token, "code": next_code},
    )
    assert done.status_code == 200 and "access_token" in done.json()

    reused = env.client.post(
        "/api/auth/totp/verify",
        json={"challenge_token": challenge_token, "code": totp.hotp_code(secret, totp.totp_step() + 2)},
    )
    assert reused.status_code == 401

    # 关闭 TOTP（支持当前密码）
    disabled = env.client.post(
        "/api/me/totp/disable",
        headers=_bearer(done.json()["access_token"]),
        json={"current_password": "Alice-Pass-2026!"},
    )
    assert disabled.status_code == 200 and disabled.json()["enabled"] is False


def test_pat_crud_scopes_restrictions_and_admin_user_ops(env):
    admin_tok = _login(env.client, "admin", "Admin-Pass-2026!").json()["access_token"]
    env.client.cookies.clear()
    alice_tok = _login(env.client, "alice", "Alice-Pass-2026!").json()["access_token"]
    env.client.cookies.clear()

    # PAT 创建（仅返回一次原文）、列表脱敏、read scope 限制、安全路径屏蔽、吊销
    created = env.client.post(
        "/api/me/tokens",
        headers=_bearer(alice_tok),
        json={"name": "CI Reader", "scopes": ["read"], "expires_in_days": 30},
    )
    assert created.status_code == 201
    pat_id = created.json()["id"]
    raw_pat = created.json()["token"]
    assert raw_pat.startswith("pat_")

    listed = env.client.get("/api/me/tokens", headers=_bearer(alice_tok)).json()
    assert len(listed) == 1 and "token" not in listed[0]

    assert env.client.get("/api/me", headers=_bearer(raw_pat)).status_code == 200
    assert env.client.post("/api/domains", headers=_bearer(raw_pat), json={}).status_code == 403
    assert env.client.get("/api/me/tokens", headers=_bearer(raw_pat)).status_code == 403
    assert env.client.post("/api/auth/refresh", headers=_bearer(raw_pat)).status_code == 403

    assert env.client.delete(f"/api/me/tokens/{pat_id}", headers=_bearer(alice_tok)).status_code == 200
    assert env.client.get("/api/me", headers=_bearer(raw_pat)).status_code == 401

    # 过期 PAT 拒绝
    with env.factory.begin() as s:
        s.add(
            PersonalAccessToken(
                id="pat-exp",
                user_id="u-alice",
                name="expired",
                token_prefix="pat_expired",
                token_hash=security.hash_pat_token("pat_expired_token_value"),
                scopes=["read"],
                expires_at=utcnow() - timedelta(minutes=5),
            )
        )
    assert env.client.get("/api/me", headers=_bearer("pat_expired_token_value")).status_code == 401

    # Admin 用户搜索（大小写/邮箱/LIKE 转义）
    found = env.client.get("/api/users?q=ALICE@example", headers=_bearer(admin_tok))
    assert found.status_code == 200 and [u["username"] for u in found.json()] == ["alice"]
    assert env.client.get("/api/users?q=%", headers=_bearer(admin_tok)).json() == []

    # Admin 创建用户自动归一化用户名
    created_u = env.client.post(
        "/api/users",
        headers=_bearer(admin_tok),
        json={"username": " Carol.Lee ", "display_name": "Carol", "password": "Carol-Pass-2026!", "roles": ["analyst"]},
    )
    assert created_u.status_code == 201 and created_u.json()["username"] == "carol.lee"

    # Admin 随机重置密码 + 强制下线
    reset = env.client.post("/api/users/u-alice/reset-password", headers=_bearer(admin_tok))
    assert reset.status_code == 200
    temp_pw = reset.json()["temporary_password"]
    assert len(temp_pw) >= 16
    assert reset.json()["user"]["must_change_password"] is True
    assert env.client.get("/api/me", headers=_bearer(alice_tok)).status_code == 401

    # 用临时密码登录后必须改密
    relog = _login(env.client, "alice", temp_pw)
    assert relog.status_code == 200 and relog.json()["must_change_password"] is True
    new_tok = relog.json()["access_token"]
    env.client.cookies.clear()

    # 强制下线使 token 失效
    assert env.client.post("/api/users/u-alice/revoke-sessions", headers=_bearer(admin_tok)).status_code == 200
    assert env.client.get("/api/me", headers=_bearer(new_tok)).status_code == 401


@pytest.mark.parametrize(
    "login_down,callback_down,replay_down",
    [
        (True, True, True),  # DOWN -> DOWN
        (False, False, True),  # UP -> DOWN
        (False, True, True),  # UP(login) -> DOWN(callback) -> DOWN(replay)
        (True, True, False),  # DOWN -> UP
        (True, False, True),  # DOWN(login) -> UP(callback) -> DOWN(replay)
        (False, False, False),  # UP -> UP
    ],
)
def test_oidc_state_replay_rejected_across_redis_outage_transitions(
    env, monkeypatch, login_down, callback_down, replay_down
):
    admin_tok = _login(env.client, "admin", "Admin-Pass-2026!").json()["access_token"]
    env.client.cookies.clear()

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk["kid"] = "k1"
    public_jwk["use"] = "sig"
    public_jwk["alg"] = "RS256"

    issuer = "https://idp.example.com"
    client_id = "avelis-client"

    assert (
        env.client.put(
            "/api/settings/auth",
            headers=_bearer(admin_tok),
            json={
                "local_login_enabled": True,
                "forward_auth_enabled": False,
                "forward_auth_header": "X-Forwarded-User",
                "forward_auth_auto_create": True,
                "oidc": {
                    "enabled": True,
                    "provider_name": "Corp IdP",
                    "issuer_url": issuer,
                    "client_id": client_id,
                    "client_secret": "oidc-secret-9999",
                    "scopes": "openid profile email",
                    "auto_create_user": True,
                    "default_role": "analyst",
                },
                "branding": {"brand_name": "Avelis"},
            },
        ).status_code
        == 200
    )

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": issuer,
                    "authorization_endpoint": f"{issuer}/authorize",
                    "token_endpoint": f"{issuer}/token",
                    "jwks_uri": f"{issuer}/jwks",
                },
            )
        if request.url.path == "/jwks":
            return httpx.Response(200, json={"keys": [public_jwk]})
        if request.url.path == "/token":
            now_ts = int(time.time())
            id_token = jwt.encode(
                {
                    "iss": issuer,
                    "sub": "ext-replay-001",
                    "aud": client_id,
                    "iat": now_ts,
                    "exp": now_ts + 300,
                    "nonce": captured["nonce"],
                    "preferred_username": "replay.user",
                    "name": "Replay User",
                    "email": "replay@example.com",
                },
                private_key,
                algorithm="RS256",
                headers={"kid": "k1"},
            )
            return httpx.Response(200, json={"access_token": "idp-at", "id_token": id_token, "token_type": "Bearer"})
        return httpx.Response(404)

    monkeypatch.setattr(auth_config, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    env.store.fail = login_down
    login_resp = env.client.get("/api/auth/oidc/login?next=/lineage", follow_redirects=False)
    assert login_resp.status_code == 302
    qs = parse_qs(urlsplit(login_resp.headers["location"]).query)
    state = qs["state"][0]
    captured["nonce"] = qs["nonce"][0]
    tx_cookie = login_resp.cookies.get(auth._OIDC_TX_COOKIE)
    assert tx_cookie

    env.store.fail = callback_down
    cb = env.client.get(f"/api/auth/oidc/callback?code=auth-code-1&state={state}", follow_redirects=False)
    assert cb.status_code == 302
    assert cb.headers["location"] == "/lineage"
    assert "avelis_access" in cb.cookies

    # 用捕获的 tx cookie 重放同一 state：无论 Redis 是否可用或期间切换状态，均必须拒绝且不得签发 Cookie
    env.client.cookies.clear()
    env.client.cookies.set(auth._OIDC_TX_COOKIE, tx_cookie, path="/api/auth/oidc")
    env.store.fail = replay_down
    replay = env.client.get(f"/api/auth/oidc/callback?code=auth-code-1&state={state}", follow_redirects=False)
    assert replay.status_code == 302
    assert replay.headers["location"] == "/login?error=oidc_state_invalid"
    assert "avelis_access" not in replay.cookies
    assert "avelis_refresh" not in replay.cookies
    assert "avelis_access" not in env.client.cookies
    env.store.fail = False


def test_totp_challenge_reuse_rejected_without_redis_and_spent_challenge_does_not_increment_failures(env, monkeypatch):
    base_ts = time.time()
    clock = [base_ts]
    monkeypatch.setattr(totp.time, "time", lambda: clock[0])

    alice_tok = _login(env.client, "alice", "Alice-Pass-2026!").json()["access_token"]
    env.client.cookies.clear()

    secret = env.client.post("/api/me/totp/enroll", headers=_bearer(alice_tok)).json()["secret"]
    assert (
        env.client.post(
            "/api/me/totp/verify", headers=_bearer(alice_tok), json={"code": totp.hotp_code(secret, totp.totp_step())}
        ).status_code
        == 200
    )

    # 场景 A：Redis 全程不可用时，challenge_token 仅能使用一次（推进 30s 至下一有效 TOTP 步重放也必须返回 401）
    env.store.fail = True
    challenge_a = _login(env.client, "alice", "Alice-Pass-2026!").json()["detail"]["challenge_token"]
    clock[0] += 30
    ok_a = env.client.post(
        "/api/auth/totp/verify",
        json={"challenge_token": challenge_a, "code": totp.hotp_code(secret, totp.totp_step())},
    )
    assert ok_a.status_code == 200 and "access_token" in ok_a.json()

    clock[0] += 30
    reused_a = env.client.post(
        "/api/auth/totp/verify",
        json={"challenge_token": challenge_a, "code": totp.hotp_code(secret, totp.totp_step())},
    )
    assert reused_a.status_code == 401 and "已使用" in reused_a.json()["detail"]

    # 切回 Redis 在线后，使用已消费的 challenge_token 提交错误验证码也不得累加 totp-fail 限流计数
    env.store.fail = False
    for _ in range(6):
        wrong_with_spent = env.client.post(
            "/api/auth/totp/verify",
            json={"challenge_token": challenge_a, "code": "000000"},
        )
        assert wrong_with_spent.status_code == 401 and "已使用" in wrong_with_spent.json()["detail"]
    assert cache.rate_limit_locked_seconds("totp-fail", "u-alice", 5) == 0
    assert env.store.get("avelis:totp-fail:u-alice") is None

    # 场景 B：首次验证时 Redis 在线，重放时 Redis 掉线，仍必须拒绝重放
    challenge_b = _login(env.client, "alice", "Alice-Pass-2026!").json()["detail"]["challenge_token"]
    clock[0] += 30
    ok_b = env.client.post(
        "/api/auth/totp/verify",
        json={"challenge_token": challenge_b, "code": totp.hotp_code(secret, totp.totp_step())},
    )
    assert ok_b.status_code == 200
    env.store.fail = True
    clock[0] += 30
    reused_b = env.client.post(
        "/api/auth/totp/verify",
        json={"challenge_token": challenge_b, "code": totp.hotp_code(secret, totp.totp_step())},
    )
    assert reused_b.status_code == 401 and "已使用" in reused_b.json()["detail"]
    env.store.fail = False


def test_local_once_fallback_cache_is_bounded_and_prunes_expired(env, monkeypatch):
    env.store.fail = True
    now = [1000.0]
    monkeypatch.setattr(cache.time, "monotonic", lambda: now[0])

    assert cache.try_set_once("test-ttl", "k1", 5) is True
    assert cache.is_marked("test-ttl", "k1") is True
    assert cache.try_set_once("test-ttl", "k1", 5) is False

    # TTL 过期后自动清理并允许再次写入
    now[0] = 1006.0
    assert cache.is_marked("test-ttl", "k1") is False
    assert cache.try_set_once("test-ttl", "k1", 5) is True

    # 超出容量上限时淘汰最旧键，本地缓存大小有界
    for idx in range(cache._LOCAL_ONCE_MAX_ENTRIES + 50):
        assert cache.try_set_once("test-cap", f"item-{idx}", 600) is True
    assert len(cache._LOCAL_ONCE_MARKERS) <= cache._LOCAL_ONCE_MAX_ENTRIES
    env.store.fail = False


def test_resolve_or_create_external_user_recovers_from_concurrent_integrity_error(env):
    issuer = "https://idp.example.com"
    sub = "concurrent-sub-1"

    with env.factory.begin() as seed_session:
        winner, err = auth_config.resolve_or_create_external_user(
            seed_session,
            provider="oidc",
            issuer=issuer,
            subject=sub,
            preferred_username="race.user",
            display_name="Winner User",
            email="race@example.com",
            auto_create=True,
            default_role="analyst",
        )
        assert winner is not None and err is None
        winner_id = winner.id

    with env.factory.begin() as loser_session:
        # 模拟并发竞态：初始查询未看到另一事务已提交的同 (issuer, sub) 行，随后插入触发 IntegrityError
        original_scalar = loser_session.scalar
        first_lookup = True

        def simulated_race_scalar(stmt, *args, **kwargs):
            nonlocal first_lookup
            if first_lookup and "external_issuer" in str(stmt):
                first_lookup = False
                return None
            return original_scalar(stmt, *args, **kwargs)

        loser_session.scalar = simulated_race_scalar
        recovered, err = auth_config.resolve_or_create_external_user(
            loser_session,
            provider="oidc",
            issuer=issuer,
            subject=sub,
            preferred_username="race.user",
            display_name="Updated Display",
            email="race@example.com",
            auto_create=True,
            default_role="analyst",
        )
        assert err is None
        assert recovered is not None
        assert recovered.id == winner_id
        assert recovered.display_name == "Updated Display"


def test_resolve_or_create_external_user_concurrent_distinct_subjects_with_same_username(tmp_path, monkeypatch):
    db_path = tmp_path / "jit_race.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    metadata = MetaData()
    for table in Base.metadata.tables.values():
        copied = table.to_metadata(metadata)
        for col in copied.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()
    metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)

    barrier = threading.Barrier(2)
    orig_alloc = auth_config._allocate_jit_username

    def synced_alloc(session, candidate):
        allocated = orig_alloc(session, candidate)
        barrier.wait(timeout=5)
        return allocated

    monkeypatch.setattr(auth_config, "_allocate_jit_username", synced_alloc)

    results: dict[str, tuple[str, str]] = {}
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(sub: str) -> None:
        try:
            with factory.begin() as sess:
                user, err = auth_config.resolve_or_create_external_user(
                    sess,
                    provider="oidc",
                    issuer="https://idp.example.com",
                    subject=sub,
                    preferred_username="info",
                    display_name=sub,
                    email=f"{sub}@example.com",
                    auto_create=True,
                    default_role="analyst",
                )
                assert err is None and user is not None
                with lock:
                    results[sub] = (user.id, user.username)
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            with lock:
                errors.append(exc)

    t1 = threading.Thread(target=worker, args=("sub-1",))
    t2 = threading.Thread(target=worker, args=("sub-2",))
    try:
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors, f"Unexpected worker errors: {errors}"
        assert set(results.keys()) == {"sub-1", "sub-2"}
        usernames = {results["sub-1"][1], results["sub-2"][1]}
        assert len(usernames) == 2
        assert "info" in usernames
        assert any(name.startswith("info_") for name in usernames)

        with factory() as verify_sess:
            users = verify_sess.scalars(select(User).order_by(User.external_sub)).all()
            assert [u.external_sub for u in users] == ["sub-1", "sub-2"]
            assert len({u.username for u in users}) == 2
            roles = verify_sess.scalars(select(UserRole)).all()
            assert len(roles) == 2
            creates = verify_sess.scalars(select(AuditEvent).where(AuditEvent.action == "user.create")).all()
            assert len(creates) == 2
    finally:
        engine.dispose()


def test_concurrent_local_vs_external_username_collision_http_endpoints(env, monkeypatch):
    admin_tok = _login(env.client, "admin", "Admin-Pass-2026!").json()["access_token"]
    env.client.cookies.clear()

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk["kid"] = "k1"
    public_jwk["use"] = "sig"
    public_jwk["alg"] = "RS256"
    issuer = "https://idp.example.com"
    client_id = "avelis-client"

    assert (
        env.client.put(
            "/api/settings/auth",
            headers=_bearer(admin_tok),
            json={
                "local_login_enabled": True,
                "forward_auth_enabled": True,
                "forward_auth_header": "X-Forwarded-User",
                "forward_auth_auto_create": True,
                "forward_auth_secret": "proxy-shared-secret",
                "oidc": {
                    "enabled": True,
                    "provider_name": "Corp IdP",
                    "issuer_url": issuer,
                    "client_id": client_id,
                    "client_secret": "oidc-secret-9999",
                    "scopes": "openid profile email",
                    "auto_create_user": True,
                    "default_role": "analyst",
                },
                "branding": {"brand_name": "Avelis"},
            },
        ).status_code
        == 200
    )

    orig_alloc = auth_config._allocate_jit_username

    def inject_concurrent_local_user(local_username: str):
        inserted = False

        def wrapped(session, candidate):
            nonlocal inserted
            allocated = orig_alloc(session, candidate)
            if not inserted:
                inserted = True
                with env.factory.begin() as race_sess:
                    race_sess.add(
                        User(
                            username=local_username,
                            display_name=f"Local {local_username}",
                            password_hash="!",
                            auth_provider="local",
                            is_active=True,
                            must_change_password=False,
                        )
                    )
            return allocated

        return wrapped

    # 1. Forward Auth 并发与本地用户 mary 用户名碰撞：不得 HTTP 500，且不劫持本地账号
    monkeypatch.setattr(auth_config, "_allocate_jit_username", inject_concurrent_local_user("mary"))
    fwd = env.client.get(
        "/api/auth/forward",
        headers={"X-Forwarded-User": "mary", "X-Forwarded-Auth-Secret": "proxy-shared-secret"},
    )
    assert fwd.status_code == 200, fwd.text
    me_fwd = env.client.get("/api/me")
    assert me_fwd.status_code == 200
    assert me_fwd.json()["username"].startswith("mary_")
    assert me_fwd.json()["auth_provider"] == "forward_auth"
    env.client.cookies.clear()

    # 2. OIDC Callback 并发与本地用户 zoe 用户名碰撞：不得 HTTP 500，且不劫持本地账号
    captured: dict[str, str] = {}

    def oidc_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": issuer,
                    "authorization_endpoint": f"{issuer}/authorize",
                    "token_endpoint": f"{issuer}/token",
                    "jwks_uri": f"{issuer}/jwks",
                },
            )
        if request.url.path == "/jwks":
            return httpx.Response(200, json={"keys": [public_jwk]})
        if request.url.path == "/token":
            now_ts = int(time.time())
            id_token = jwt.encode(
                {
                    "iss": issuer,
                    "sub": "ext-zoe-001",
                    "aud": client_id,
                    "iat": now_ts,
                    "exp": now_ts + 300,
                    "nonce": captured["nonce"],
                    "preferred_username": "zoe",
                    "name": "Zoe External",
                    "email": "zoe@example.com",
                },
                private_key,
                algorithm="RS256",
                headers={"kid": "k1"},
            )
            return httpx.Response(200, json={"access_token": "idp-at", "id_token": id_token, "token_type": "Bearer"})
        return httpx.Response(404)

    monkeypatch.setattr(
        auth_config, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(oidc_handler))
    )
    login_resp = env.client.get("/api/auth/oidc/login?next=/lineage", follow_redirects=False)
    assert login_resp.status_code == 302
    qs = parse_qs(urlsplit(login_resp.headers["location"]).query)
    captured["nonce"] = qs["nonce"][0]

    monkeypatch.setattr(auth_config, "_allocate_jit_username", inject_concurrent_local_user("zoe"))
    cb = env.client.get(
        f"/api/auth/oidc/callback?code=auth-code-zoe&state={qs['state'][0]}",
        follow_redirects=False,
    )
    assert cb.status_code == 302
    assert cb.headers["location"] == "/lineage"
    me_oidc = env.client.get("/api/me")
    assert me_oidc.status_code == 200
    assert me_oidc.json()["username"].startswith("zoe_")
    assert me_oidc.json()["auth_provider"] == "oidc"

    with env.factory() as verify_sess:
        mary_local = verify_sess.scalar(select(User).where(User.username == "mary"))
        zoe_local = verify_sess.scalar(select(User).where(User.username == "zoe"))
        assert mary_local is not None and mary_local.auth_provider == "local"
        assert zoe_local is not None and zoe_local.auth_provider == "local"
        creates = verify_sess.scalars(select(AuditEvent).where(AuditEvent.action == "user.create")).all()
        assert len(creates) == 2


def test_resolve_or_create_external_user_raises_after_max_retry_attempts(env, monkeypatch):
    monkeypatch.setattr(auth_config, "_allocate_jit_username", lambda _s, _c: "alice")
    monkeypatch.setattr(auth_config, "_allocate_retry_jit_username", lambda _s, _c, _tried: "alice")

    with env.factory() as sess:
        with pytest.raises(IntegrityError):
            auth_config.resolve_or_create_external_user(
                sess,
                provider="oidc",
                issuer="https://idp.example.com",
                subject="exhaust-sub",
                preferred_username="alice",
                display_name="Alice External",
                email="alice.ext@example.com",
                auto_create=True,
                default_role="analyst",
            )
        sess.rollback()

    with env.factory() as verify_sess:
        assert verify_sess.scalars(select(AuditEvent).where(AuditEvent.action == "user.create")).all() == []


@pytest.mark.parametrize("provider", ["oidc", "forward_auth"])
@pytest.mark.parametrize("route_suffix", ["password", "reset-password"])
def test_password_reset_routes_reject_external_accounts_without_mutating(env, provider, route_suffix):
    admin_tok = _login(env.client, "admin", "Admin-Pass-2026!").json()["access_token"]
    env.client.cookies.clear()

    ext_id = f"u-ext-{provider}-{route_suffix}"
    with env.factory.begin() as s:
        s.add(
            User(
                id=ext_id,
                username=f"ext_{provider}_{route_suffix.replace('-', '_')}",
                display_name="External User",
                email="ext@example.com",
                password_hash="!",
                auth_provider=provider,
                external_issuer="https://idp.example.com" if provider == "oidc" else "forward_auth",
                external_sub=ext_id,
                is_active=True,
                must_change_password=False,
            )
        )

    url = f"/api/users/{ext_id}/{route_suffix}"
    payload = {"new_password": "BrandNew-Pass-2026!"} if route_suffix == "password" else None
    resp = env.client.post(url, headers=_bearer(admin_tok), json=payload)
    assert resp.status_code == 409, resp.text
    assert "外部认证账号" in resp.json()["detail"]

    with env.factory() as s:
        u = s.get(User, ext_id)
        assert u is not None
        assert u.password_hash == "!"
        assert u.must_change_password is False
        assert u.password_changed_at is None
        assert u.sessions_valid_after is None

    # 本地账号两条重置入口均正常工作
    local_payload = {"new_password": "Alice-Reset-2026!"} if route_suffix == "password" else None
    local_resp = env.client.post(f"/api/users/u-alice/{route_suffix}", headers=_bearer(admin_tok), json=local_payload)
    assert local_resp.status_code == 200, local_resp.text


@pytest.mark.parametrize("revocation_action", ["self_password", "admin_password", "admin_reset", "revoke_sessions"])
def test_pending_totp_challenge_is_invalidated_by_session_revocation(env, revocation_action):
    admin_tok = _login(env.client, "admin", "Admin-Pass-2026!").json()["access_token"]
    env.client.cookies.clear()
    alice_tok = _login(env.client, "alice", "Alice-Pass-2026!").json()["access_token"]
    env.client.cookies.clear()

    # 启用 Alice 的 TOTP
    enroll = env.client.post("/api/me/totp/enroll", headers=_bearer(alice_tok))
    assert enroll.status_code == 200
    secret = enroll.json()["secret"]
    step0 = totp.totp_step()
    assert (
        env.client.post(
            "/api/me/totp/verify",
            headers=_bearer(alice_tok),
            json={"code": totp.hotp_code(secret, step0)},
        ).status_code
        == 200
    )

    # 先发起登录获取待验证 TOTP challenge
    chal_resp = _login(env.client, "alice", "Alice-Pass-2026!")
    assert chal_resp.status_code == 403
    old_challenge = chal_resp.json()["detail"]["challenge_token"]

    # 执行触发会话撤销的操作
    current_pw = "Alice-Pass-2026!"
    if revocation_action == "self_password":
        new_pw = "BrandNew-Valid-2026!"
        assert (
            env.client.post(
                "/api/me/password",
                headers=_bearer(alice_tok),
                json={"current_password": current_pw, "new_password": new_pw},
            ).status_code
            == 200
        )
        current_pw = new_pw
    elif revocation_action == "admin_password":
        new_pw = "AdminSet-Valid-2026!"
        assert (
            env.client.post(
                "/api/users/u-alice/password",
                headers=_bearer(admin_tok),
                json={"new_password": new_pw},
            ).status_code
            == 200
        )
        current_pw = new_pw
    elif revocation_action == "admin_reset":
        reset_res = env.client.post("/api/users/u-alice/reset-password", headers=_bearer(admin_tok))
        assert reset_res.status_code == 200
        current_pw = reset_res.json()["temporary_password"]
    else:
        assert env.client.post("/api/users/u-alice/revoke-sessions", headers=_bearer(admin_tok)).status_code == 200

    # 旧待完成 TOTP 挑战即使提供正确验证码也被拒绝
    rejected = env.client.post(
        "/api/auth/totp/verify",
        json={"challenge_token": old_challenge, "code": totp.hotp_code(secret, step0 + 1)},
    )
    assert rejected.status_code == 401
    assert "会话已失效" in rejected.json()["detail"]

    # 使用最新凭据重新发起登录与 TOTP 校验不受影响（前次挑战因会话失效未消耗 step0 + 1）
    fresh_chal = _login(env.client, "alice", current_pw)
    assert fresh_chal.status_code == 403
    fresh_done = env.client.post(
        "/api/auth/totp/verify",
        json={
            "challenge_token": fresh_chal.json()["detail"]["challenge_token"],
            "code": totp.hotp_code(secret, step0 + 1),
        },
    )
    assert fresh_done.status_code == 200, fresh_done.text
    assert "access_token" in fresh_done.json()


def test_totp_disable_password_branch_shares_rate_limit_and_skips_hashing_when_locked(env, monkeypatch):
    alice_tok = _login(env.client, "alice", "Alice-Pass-2026!").json()["access_token"]
    env.client.cookies.clear()

    enroll = env.client.post("/api/me/totp/enroll", headers=_bearer(alice_tok)).json()
    secret = enroll["secret"]
    step0 = totp.totp_step()
    assert (
        env.client.post(
            "/api/me/totp/verify",
            headers=_bearer(alice_tok),
            json={"code": totp.hotp_code(secret, step0)},
        ).status_code
        == 200
    )

    # 前 5 次错误密码均返回 403
    for _ in range(5):
        bad = env.client.post(
            "/api/me/totp/disable",
            headers=_bearer(alice_tok),
            json={"current_password": "wrong-password"},
        )
        assert bad.status_code == 403

    # 第 6 次被限流 429 且不再执行密码哈希校验，同时与 /api/me/password 共享同一限流桶
    called = False
    orig_verify = auth.verify_password

    def tracked_verify(*args, **kwargs):
        nonlocal called
        called = True
        return orig_verify(*args, **kwargs)

    monkeypatch.setattr(auth, "verify_password", tracked_verify)
    locked_resp = env.client.post(
        "/api/me/totp/disable",
        headers=_bearer(alice_tok),
        json={"current_password": "Alice-Pass-2026!"},
    )
    assert locked_resp.status_code == 429
    assert int(locked_resp.headers["Retry-After"]) > 0
    assert called is False

    pw_locked = env.client.post(
        "/api/me/password",
        headers=_bearer(alice_tok),
        json={"current_password": "Alice-Pass-2026!", "new_password": "Alice-NewPass-2026!"},
    )
    assert pw_locked.status_code == 429

    # 锁定期间使用 TOTP 验证码分支仍可正常工作（不走密码限流桶）
    disabled_by_code = env.client.post(
        "/api/me/totp/disable",
        headers=_bearer(alice_tok),
        json={"code": totp.hotp_code(secret, step0 + 1)},
    )
    assert disabled_by_code.status_code == 200 and disabled_by_code.json()["enabled"] is False


def test_forward_auth_checks_direct_proxy_peer_before_forwarded_headers(env):
    import socket

    import uvicorn

    from avelis import server

    admin_tok = _login(env.client, "admin", "Admin-Pass-2026!").json()["access_token"]
    env.client.cookies.clear()
    assert (
        env.client.put(
            "/api/settings/auth",
            headers=_bearer(admin_tok),
            json={
                "local_login_enabled": True,
                "forward_auth_enabled": True,
                "forward_auth_header": "X-Forwarded-User",
                "forward_auth_auto_create": True,
                "clear_forward_auth_secret": True,
                "oidc": {"enabled": False},
                "branding": {"brand_name": "Avelis"},
            },
        ).status_code
        == 200
    )

    proxy_settings = replace(env.settings, forwarded_allow_ips="10.0.0.10")
    env.set_settings(proxy_settings)
    proxy_app = app_module.create_app(settings=proxy_settings)
    proxy_app.dependency_overrides[deps.get_session] = env.app.dependency_overrides[deps.get_session]

    # 可信直连代理 (10.0.0.10) 带上 X-Forwarded-For 访客 IP 203.0.113.50 与 X-Forwarded-Proto: https -> 放行
    with TestClient(proxy_app, client=("10.0.0.10", 54321)) as trusted_client:
        ok = trusted_client.get(
            "/api/auth/forward",
            headers={
                "X-Forwarded-User": "Proxy.Visitor",
                "X-Forwarded-For": "203.0.113.50",
                "X-Forwarded-Proto": "https",
            },
        )
        assert ok.status_code == 200, ok.text
        set_cookies = ok.headers.get_list("set-cookie")
        assert any("avelis_access=" in c and "; Secure" in c for c in set_cookies)

    # 非可信直连客户端 (198.51.100.99) 即使伪造 X-Forwarded-For: 10.0.0.10 -> 403 拒绝
    untrusted_app = app_module.create_app(settings=proxy_settings)
    untrusted_app.dependency_overrides[deps.get_session] = env.app.dependency_overrides[deps.get_session]
    with TestClient(untrusted_app, client=("198.51.100.99", 54321)) as untrusted_client:
        denied = untrusted_client.get(
            "/api/auth/forward",
            headers={"X-Forwarded-User": "Spoofed.User", "X-Forwarded-For": "10.0.0.10"},
        )
        assert denied.status_code == 403

    # 真实 Uvicorn TCP 服务 + server.uvicorn_options：回环代理 (127.0.0.1) 转发 X-Forwarded-For 仍可通过 Forward Auth
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    loopback_settings = replace(
        env.settings,
        forwarded_allow_ips="127.0.0.1",
        host="127.0.0.1",
        port=port,
        workers=1,
    )
    env.set_settings(loopback_settings)
    real_app = app_module.create_app(settings=loopback_settings)
    real_app.dependency_overrides[deps.get_session] = env.app.dependency_overrides[deps.get_session]

    opts = server.uvicorn_options(loopback_settings)
    opts.pop("log_config", None)
    config_obj = uvicorn.Config(real_app, **opts)
    uv_server = uvicorn.Server(config_obj)
    uv_server.install_signal_handlers = lambda: None
    thread = threading.Thread(target=uv_server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while not uv_server.started and time.time() < deadline:
            time.sleep(0.05)
        assert uv_server.started
        resp = httpx.get(
            f"http://127.0.0.1:{port}/api/auth/forward",
            headers={"X-Forwarded-User": "Real.ProxyUser", "X-Forwarded-For": "203.0.113.50"},
            timeout=5.0,
        )
        assert resp.status_code == 200, resp.text
        assert "access_token" in resp.json()
    finally:
        uv_server.should_exit = True
        thread.join(timeout=5)


def test_legacy_case_distinct_usernames_resolve_deterministically_without_silent_pick(env):
    with env.factory.begin() as s:
        s.add_all(
            [
                User(
                    id="u-dup-upper",
                    username="DupUser",
                    display_name="Upper Dup",
                    password_hash=security.hash_password("Upper-Pass-2026!"),
                    must_change_password=False,
                ),
                User(
                    id="u-dup-lower",
                    username="dupuser",
                    display_name="Lower Dup",
                    password_hash=security.hash_password("Lower-Pass-2026!"),
                    must_change_password=False,
                ),
            ]
        )

    upper_ok = _login(env.client, "DupUser", "Upper-Pass-2026!")
    assert upper_ok.status_code == 200
    assert env.client.get("/api/me", headers=_bearer(upper_ok.json()["access_token"])).json()["id"] == "u-dup-upper"
    env.client.cookies.clear()

    lower_ok = _login(env.client, "dupuser", "Lower-Pass-2026!")
    assert lower_ok.status_code == 200
    assert env.client.get("/api/me", headers=_bearer(lower_ok.json()["access_token"])).json()["id"] == "u-dup-lower"
    env.client.cookies.clear()

    # 歧义大小写不静默任选一个，一律拒绝
    ambiguous = _login(env.client, "DUPUSER", "Lower-Pass-2026!")
    assert ambiguous.status_code == 401
    ambiguous2 = _login(env.client, "DUPUSER", "Upper-Pass-2026!")
    assert ambiguous2.status_code == 401
