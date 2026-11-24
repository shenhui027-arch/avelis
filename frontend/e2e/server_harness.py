"""Self-contained SQLite + InMemoryRedis harness for Playwright E2E tests."""

from __future__ import annotations

import argparse
import atexit
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
import zlib
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import JSON, MetaData, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))


class _Pipeline:
    def __init__(self, redis_inst: _InMemoryRedis) -> None:
        self._r = redis_inst
        self._ops: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __enter__(self) -> _Pipeline:
        return self

    def __exit__(self, *_args: Any) -> None:
        self._ops.clear()

    def _add(self, name: str, *args: Any, **kwargs: Any) -> _Pipeline:
        self._ops.append((name, args, kwargs))
        return self

    def get(self, key: str) -> _Pipeline:
        return self._add("get", key)

    def set(self, key: str, value: Any, **kwargs: Any) -> _Pipeline:
        return self._add("set", key, value, **kwargs)

    def exists(self, *keys: str) -> _Pipeline:
        return self._add("exists", *keys)

    def incr(self, key: str, amount: int = 1) -> _Pipeline:
        return self._add("incr", key, amount)

    def expire(self, key: str, ttl_s: int, nx: bool = False, xx: bool = False) -> _Pipeline:
        return self._add("expire", key, ttl_s, nx=nx, xx=xx)

    def ttl(self, key: str) -> _Pipeline:
        return self._add("ttl", key)

    def delete(self, *keys: str) -> _Pipeline:
        return self._add("delete", *keys)

    def execute(self) -> list[Any]:
        with self._r._lock:
            out = [getattr(self._r, op)(*args, **kwargs) for op, args, kwargs in self._ops]
        self._ops.clear()
        return out


class _InMemoryRedis:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._kv: dict[str, str] = {}
        self._exp: dict[str, float] = {}

    def _evict(self, key: str) -> None:
        exp = self._exp.get(key)
        if exp is not None and exp <= time.time():
            self._kv.pop(key, None)
            self._exp.pop(key, None)

    def ping(self) -> bool:
        return True

    def get(self, key: str) -> str | None:
        with self._lock:
            self._evict(key)
            return self._kv.get(key)

    def set(
        self, key: str, value: Any, ex: int | None = None, px: int | None = None, nx: bool = False, xx: bool = False
    ) -> bool | None:
        with self._lock:
            self._evict(key)
            has = key in self._kv
            if (nx and has) or (xx and not has):
                return None
            self._kv[key] = str(value)
            if ex is not None:
                self._exp[key] = time.time() + ex
            elif px is not None:
                self._exp[key] = time.time() + px / 1000.0
            else:
                self._exp.pop(key, None)
            return True

    def exists(self, *keys: str) -> int:
        with self._lock:
            count = 0
            for k in keys:
                self._evict(k)
                if k in self._kv:
                    count += 1
            return count

    def incr(self, key: str, amount: int = 1) -> int:
        with self._lock:
            self._evict(key)
            nxt = int(self._kv.get(key, "0")) + amount
            self._kv[key] = str(nxt)
            return nxt

    def expire(self, key: str, ttl_s: int, nx: bool = False, xx: bool = False) -> bool:
        with self._lock:
            self._evict(key)
            if key not in self._kv:
                return False
            cur = self._exp.get(key)
            if (nx and cur is not None) or (xx and cur is None):
                return False
            if ttl_s <= 0:
                self.delete(key)
                return True
            self._exp[key] = time.time() + ttl_s
            return True

    def ttl(self, key: str) -> int:
        with self._lock:
            self._evict(key)
            if key not in self._kv:
                return -2
            exp = self._exp.get(key)
            if exp is None:
                return -1
            return max(int(exp - time.time() + 0.999), 0)

    def delete(self, *keys: str) -> int:
        with self._lock:
            removed = 0
            for k in keys:
                if k in self._kv or k in self._exp:
                    removed += 1
                self._kv.pop(k, None)
                self._exp.pop(k, None)
            return removed

    def pipeline(self, transaction: bool = True) -> _Pipeline:
        return _Pipeline(self)

    def close(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


class _FlushBeforeSendMiddleware:
    """Buffer non-streaming responses until FastAPI yield dependencies finish committing."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        buffered: list[dict] = []
        streaming = False

        async def _send(msg: dict) -> None:
            nonlocal streaming
            if streaming:
                await send(msg)
                return
            if msg["type"] == "http.response.start":
                hdrs = {k.lower(): v.lower() for k, v in msg.get("headers", [])}
                if b"text/event-stream" in hdrs.get(b"content-type", b""):
                    streaming = True
                    await send(msg)
                    return
                buffered.append(msg)
                return
            if msg["type"] == "http.response.body":
                buffered.append(msg)
                return
            await send(msg)

        await self.app(scope, receive, _send)
        for msg in buffered:
            await send(msg)


def _start_mock_oidc_server() -> tuple[str, ThreadingHTTPServer]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk["kid"] = "e2e-k1"
    public_jwk["use"] = "sig"
    public_jwk["alg"] = "RS256"
    auth_codes: dict[str, str] = {}
    code_lock = threading.Lock()
    issuer_holder = {"url": ""}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: Any) -> None:
            return None

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            issuer = issuer_holder["url"]
            if parsed.path == "/.well-known/openid-configuration":
                body = json.dumps(
                    {
                        "issuer": issuer,
                        "authorization_endpoint": f"{issuer}/authorize",
                        "token_endpoint": f"{issuer}/token",
                        "jwks_uri": f"{issuer}/jwks",
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/jwks":
                body = json.dumps({"keys": [public_jwk]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/authorize":
                qs = parse_qs(parsed.query)
                redirect_uri = qs["redirect_uri"][0]
                state = qs["state"][0]
                nonce = qs["nonce"][0]
                code = f"e2e-code-{uuid.uuid4().hex}"
                with code_lock:
                    auth_codes[code] = nonce
                sep = "&" if "?" in redirect_uri else "?"
                self.send_response(302)
                self.send_header("Location", f"{redirect_uri}{sep}code={code}&state={state}")
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            if urlsplit(self.path).path != "/token":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length") or "0")
            form = parse_qs(self.rfile.read(length).decode("utf-8"))
            code = (form.get("code") or [""])[0]
            with code_lock:
                nonce = auth_codes.pop(code, "")
            now_ts = int(time.time())
            id_token = jwt.encode(
                {
                    "iss": issuer_holder["url"],
                    "sub": "oidc-sub-steward",
                    "aud": "avelis-e2e-client",
                    "iat": now_ts,
                    "exp": now_ts + 300,
                    "nonce": nonce,
                    "preferred_username": "oidc_steward",
                    "name": "OIDC Steward",
                    "email": "oidc_steward@avelis.local",
                },
                private_key,
                algorithm="RS256",
                headers={"kid": "e2e-k1"},
            )
            body = json.dumps({"access_token": "mock-oidc-at", "id_token": id_token, "token_type": "Bearer"}).encode(
                "utf-8"
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    issuer_url = f"http://127.0.0.1:{srv.server_port}"
    issuer_holder["url"] = issuer_url
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return issuer_url, srv


def _create_sqlite_db(db_path: Path, *, oidc_issuer: str):
    from avelis import db, security
    from avelis.models import AnalysisDataset, AnalysisRun, Asset, Datasource, Domain, DomainMember, User, UserRole

    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _on_connect(conn, _rec) -> None:
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()
        conn.create_function("hashtextextended", 2, lambda key, seed: zlib.crc32(str(key).encode()) ^ int(seed))
        for fn in ("pg_advisory_lock", "pg_try_advisory_lock", "pg_advisory_unlock", "pg_advisory_xact_lock"):
            conn.create_function(fn, 1, lambda _k: 1)

    meta = MetaData()
    for tbl in db.Base.metadata.tables.values():
        copied = tbl.to_metadata(meta)
        for col in copied.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()
    meta.create_all(engine)

    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory.begin() as session:
        admin = User(
            id="e2e-admin-1",
            username="admin",
            display_name="Admin User",
            email="admin@avelis.local",
            password_hash=security.hash_password("Admin-e2e-password-2026!"),
            is_active=True,
            must_change_password=False,
        )
        steward = User(
            id="e2e-steward-1",
            username="steward_user",
            display_name="Steward User",
            email="steward@avelis.local",
            password_hash=security.hash_password("Steward-e2e-password-2026!"),
            is_active=True,
            must_change_password=False,
        )
        analyst = User(
            id="e2e-analyst-1",
            username="analyst_user",
            display_name="Analyst User",
            email="analyst@avelis.local",
            password_hash=security.hash_password("Analyst-e2e-password-2026!"),
            is_active=True,
            must_change_password=False,
        )
        forced = User(
            id="e2e-forced-1",
            username="forced_user",
            display_name="Forced Change User",
            email="forced@avelis.local",
            password_hash=security.hash_password("Forced-e2e-password-2026!"),
            is_active=True,
            must_change_password=True,
        )
        kick_target = User(
            id="e2e-kick-1",
            username="kick_user",
            display_name="Kick Target",
            email="kick@avelis.local",
            password_hash=security.hash_password("Kick-e2e-password-2026!"),
            is_active=True,
            must_change_password=False,
        )
        session.add_all([admin, steward, analyst, forced, kick_target])
        session.flush()
        session.add_all(
            [
                UserRole(user_id=admin.id, role="admin"),
                UserRole(user_id=steward.id, role="steward"),
                UserRole(user_id=analyst.id, role="analyst"),
                UserRole(user_id=forced.id, role="analyst"),
                UserRole(user_id=kick_target.id, role="analyst"),
            ]
        )
        domain = Domain(
            id="e2e-domain-1",
            code="core_metrics",
            name="Core Governance",
            description="E2E governance domain",
            owner_id=admin.id,
        )
        session.add(domain)
        session.flush()
        session.add_all(
            [
                DomainMember(domain_id=domain.id, user_id=admin.id, role="owner"),
                DomainMember(domain_id=domain.id, user_id=steward.id, role="maintainer"),
                DomainMember(domain_id=domain.id, user_id=analyst.id, role="viewer"),
            ]
        )

        oidc_steward = User(
            id="e2e-oidc-steward",
            username="oidc_steward",
            display_name="OIDC Steward",
            email="oidc_steward@avelis.local",
            password_hash="!",
            auth_provider="oidc",
            external_issuer=oidc_issuer,
            external_sub="oidc-sub-steward",
            is_active=True,
            must_change_password=False,
        )
        fw_steward = User(
            id="e2e-fw-steward",
            username="fw_steward",
            display_name="Forward Auth Steward",
            email="fw_steward@avelis.local",
            password_hash="!",
            auth_provider="forward_auth",
            external_issuer="forward_auth",
            external_sub="fw_steward",
            is_active=True,
            must_change_password=False,
        )
        ds = Datasource(
            id="e2e-ds-pg",
            name="E2E Postgres",
            kind="postgres",
            host="127.0.0.1",
            port=5432,
            options={"database": "demo", "schemas": ["public"]},
            allow_exploration=True,
        )
        asset = Asset(
            id="e2e-asset-1",
            datasource_id=ds.id,
            domain_id=domain.id,
            catalog_name="demo",
            db_name="public",
            name="sales",
            kind="table",
            physical_id="e2e-sales-1",
            status="active",
        )
        session.add_all([oidc_steward, fw_steward, ds, asset])
        session.flush()
        session.add_all(
            [
                UserRole(user_id=oidc_steward.id, role="steward"),
                UserRole(user_id=fw_steward.id, role="steward"),
                DomainMember(domain_id=domain.id, user_id=oidc_steward.id, role="maintainer"),
                DomainMember(domain_id=domain.id, user_id=fw_steward.id, role="maintainer"),
            ]
        )
        for run_id, uid in (
            ("e2e-run-oidc", oidc_steward.id),
            ("e2e-run-fw", fw_steward.id),
            ("e2e-run-local", steward.id),
        ):
            session.add(
                AnalysisRun(
                    id=run_id,
                    conversation_id=run_id,
                    user_id=uid,
                    question="E2E 区域销售汇总",
                    trigger="manual",
                    status="succeeded",
                    window={},
                    plan={},
                    report={
                        "title": "E2E Export Check",
                        "summary": "区域销售数据已就绪。",
                        "claims": [],
                    },
                    verification={},
                    budget={},
                )
            )
            session.add(
                AnalysisDataset(
                    id=f"{run_id}-d1",
                    run_id=run_id,
                    seq=1,
                    kind="explore",
                    title="E2E Regional GMV",
                    inputs=[],
                    columns=["region", "gmv"],
                    rows=[["华东", 12345]],
                    truncated=False,
                    meta={"tables": [{"asset_id": asset.id}]},
                )
            )
    return engine, factory


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18100)
    args = parser.parse_args()

    tmp_dir = Path(tempfile.mkdtemp(prefix="avelis_fe_e2e_"))
    atexit.register(lambda: shutil.rmtree(tmp_dir, ignore_errors=True))

    oidc_issuer, oidc_srv = _start_mock_oidc_server()
    atexit.register(oidc_srv.shutdown)

    frontend_dist = REPO_ROOT / "frontend" / "dist"
    env_map = {
        "AVELIS_SECRET": "e2e-frontend-secret-key-0123456789abcdef",
        "AVELIS_ENV_FILE": str(tmp_dir / "nonexistent.env"),
        "AVELIS_ENVIRONMENT": "development",
        "AVELIS_COOKIE_SECURE": "false",
        "AVELIS_PUBLIC_URL": f"http://127.0.0.1:{args.port}",
        "AVELIS_FRONTEND_DIR": str(frontend_dist),
        "AVELIS_DATABASE_URL": "postgresql+psycopg://avelis:offline@127.0.0.1:15432/avelis",
        "AVELIS_REDIS_URL": "redis://127.0.0.1:16379/0",
        "AVELIS_LOGIN_MAX_FAILURES": "3",
        "AVELIS_LOGIN_LOCK_MINUTES": "2",
    }
    for k, v in env_map.items():
        os.environ[k] = v

    from avelis import cache, config, db
    from avelis.api.app import create_app

    settings = config.build_settings(env_map)
    engine, factory = _create_sqlite_db(tmp_dir / "e2e.sqlite", oidc_issuer=oidc_issuer)
    redis_mem = _InMemoryRedis()

    @contextmanager
    def _session_scope():
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    orig_settings = config.get_settings
    orig_factory = db.session_factory
    orig_scope = db.session_scope
    orig_engine = db.get_engine
    orig_client = cache.client

    for mod in list(sys.modules.values()):
        name = getattr(mod, "__name__", "")
        if not name.startswith("avelis"):
            continue
        if getattr(mod, "get_settings", None) is orig_settings:
            setattr(mod, "get_settings", lambda: settings)
        if getattr(mod, "session_factory", None) is orig_factory:
            setattr(mod, "session_factory", lambda: factory)
        if getattr(mod, "session_scope", None) is orig_scope:
            setattr(mod, "session_scope", _session_scope)
        if getattr(mod, "get_engine", None) is orig_engine:
            setattr(mod, "get_engine", lambda: engine)
        if getattr(mod, "client", None) is orig_client:
            setattr(mod, "client", lambda: redis_mem)
        if getattr(mod, "cache_client", None) is orig_client:
            setattr(mod, "cache_client", lambda: redis_mem)

    inner_app = _FlushBeforeSendMiddleware(create_app(settings=settings))

    async def app(scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope.get("path") == "/__e2e/info":
            body = json.dumps({"oidc_issuer": oidc_issuer}).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await inner_app(scope, receive, send)

    uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=False, log_level="warning")


if __name__ == "__main__":
    main()
