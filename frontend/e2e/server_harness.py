"""Self-contained SQLite + InMemoryRedis harness for Playwright E2E tests."""

from __future__ import annotations

import argparse
import atexit
import os
import shutil
import sys
import tempfile
import threading
import time
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import uvicorn
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

    def set(self, key: str, value: Any, ex: int | None = None, px: int | None = None, nx: bool = False, xx: bool = False) -> bool | None:
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


def _create_sqlite_db(db_path: Path):
    from avelis import db, security
    from avelis.models import Domain, DomainMember, User, UserRole

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
        session.add_all([
            UserRole(user_id=admin.id, role="admin"),
            UserRole(user_id=steward.id, role="steward"),
            UserRole(user_id=analyst.id, role="analyst"),
            UserRole(user_id=forced.id, role="analyst"),
            UserRole(user_id=kick_target.id, role="analyst"),
        ])
        domain = Domain(
            id="e2e-domain-1",
            code="core_metrics",
            name="Core Governance",
            description="E2E governance domain",
            owner_id=admin.id,
        )
        session.add(domain)
        session.flush()
        session.add_all([
            DomainMember(domain_id=domain.id, user_id=admin.id, role="owner"),
            DomainMember(domain_id=domain.id, user_id=steward.id, role="maintainer"),
            DomainMember(domain_id=domain.id, user_id=analyst.id, role="viewer"),
        ])
    return engine, factory


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18100)
    args = parser.parse_args()

    tmp_dir = Path(tempfile.mkdtemp(prefix="avelis_fe_e2e_"))
    atexit.register(lambda: shutil.rmtree(tmp_dir, ignore_errors=True))

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
    engine, factory = _create_sqlite_db(tmp_dir / "e2e.sqlite")
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

    app = _FlushBeforeSendMiddleware(create_app(settings=settings))
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
