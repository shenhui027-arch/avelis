"""部署入口的静态托管与就绪检查：只使用临时文件和内存替身。"""

from __future__ import annotations

import asyncio
import importlib
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from avelis.api import app as api_app
from avelis.config import build_settings


@pytest.fixture
def configure(monkeypatch):
    def build(**env):
        settings = build_settings({"AVELIS_SECRET": "offline-unit-test-secret-0123456789", **env})
        monkeypatch.setattr(api_app, "get_settings", lambda: settings)
        return api_app.create_app()

    return build


@pytest.fixture
def dist(tmp_path):
    root = tmp_path / "frontend" / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<main>前端入口</main>", encoding="utf-8")
    (root / "assets" / "app-abc.js").write_text("console.log('asset')", encoding="utf-8")
    (root / "assets" / "plain.js").write_text("console.log('plain')", encoding="utf-8")
    (root / "favicon.svg").write_text("<svg>favicon</svg>", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("outside-dist-secret", encoding="utf-8")
    (root.parent / "pyproject.toml").write_text("outside-dist-secret", encoding="utf-8")
    (root.parent / "x").write_text("outside-dist-secret", encoding="utf-8")
    return root


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("path", ["/", "/index.html", "/lineage", "/ask/example"])
def test_frontend_entry_and_spa_routes(configure, dist, method, path):
    with TestClient(configure(AVELIS_FRONTEND_DIR=str(dist))) as client:
        response = client.request(method, path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["content-type"].startswith("text/html")
    assert response.text == ("<main>前端入口</main>" if method == "GET" else "")


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_hashed_assets_have_immutable_cache(configure, dist, method):
    with TestClient(configure(AVELIS_FRONTEND_DIR=str(dist))) as client:
        response = client.request(method, "/assets/app-abc.js")
        plain = client.get("/assets/plain.js")
        favicon = client.get("/favicon.svg")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.text == ("console.log('asset')" if method == "GET" else "")
    assert plain.status_code == 200
    assert "immutable" not in plain.headers.get("cache-control", "")
    assert favicon.status_code == 200
    assert favicon.text == "<svg>favicon</svg>"
    assert "immutable" not in favicon.headers.get("cache-control", "")


@pytest.mark.parametrize("path", ["/api", "/api/", "/api/not-exist"])
@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_api_unknown_paths_never_fall_back_to_frontend(configure, dist, path, method):
    with TestClient(configure(AVELIS_FRONTEND_DIR=str(dist))) as client:
        response = client.request(method, path)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert "detail" in response.json()


def test_frontend_preserves_api_method_not_allowed(configure, dist):
    with TestClient(configure(AVELIS_FRONTEND_DIR=str(dist))) as client:
        assert client.post("/api/health").status_code == 405
        assert client.get("/api/auth/token").status_code == 405


@pytest.mark.parametrize(
    "path", ["/..%2F..%2Fpyproject.toml", "/assets/../../x", "/%2e%2e/pyproject.toml", "/assets/%2e%2e/%2e%2e/x"]
)
def test_frontend_cannot_read_outside_dist(configure, dist, path):
    with TestClient(configure(AVELIS_FRONTEND_DIR=str(dist))) as client:
        response = client.get(path)
    assert response.status_code in (200, 404)
    assert "outside-dist-secret" not in response.text


def test_frontend_rejects_symlink_outside_dist(configure, dist):
    target = dist / "assets" / "outside.txt"
    try:
        target.symlink_to(dist.parent / "pyproject.toml")
    except OSError:
        pytest.skip("当前 Windows 账户没有创建符号链接的权限")
    with TestClient(configure(AVELIS_FRONTEND_DIR=str(dist))) as client:
        response = client.get("/assets/outside.txt")
    assert response.status_code == 404
    assert "outside-dist-secret" not in response.text


def test_frontend_is_disabled_by_default(configure):
    with TestClient(configure()) as client:
        assert client.get("/").status_code == 404
        assert client.head("/").status_code == 404
        assert client.get("/api/health").json() == {"status": "ok"}


@pytest.mark.parametrize("missing_dir", [False, True])
def test_frontend_requires_existing_index(configure, tmp_path, missing_dir):
    root = tmp_path / "missing"
    if not missing_dir:
        root.mkdir()
    with pytest.raises(ValueError, match="index.html") as error:
        configure(AVELIS_FRONTEND_DIR=str(root))
    assert str(root) in str(error.value)


@pytest.mark.parametrize("database,redis", [(True, True), (False, True), (True, False), (False, False)])
def test_ready_checks_database_and_redis_without_exposing_errors(configure, monkeypatch, database, redis):
    checks = []

    class Connection:
        def scalar(self, statement):
            checks.append(str(statement))
            if not database:
                raise RuntimeError("postgresql://user:private-password@internal-host/database")
            return 1

    @contextmanager
    def connect():
        yield Connection()

    def ping():
        checks.append("ping")
        if not redis:
            raise RuntimeError("redis://:private-password@internal-host/0")
        return True

    monkeypatch.setattr(api_app, "get_engine", lambda: SimpleNamespace(connect=connect))
    monkeypatch.setattr(api_app, "cache_client", lambda: SimpleNamespace(ping=ping))
    with TestClient(configure()) as client:
        response = client.get("/api/health/ready")
    assert checks == ["SELECT 1", "ping"]
    if database and redis:
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
    else:
        assert response.status_code == 503
        assert response.json()["failed"] == [name for name, ok in (("database", database), ("redis", redis)) if not ok]
        assert "private-password" not in response.text
        assert "internal-host" not in response.text


def test_ready_rejects_unsuccessful_ping(configure, monkeypatch):
    @contextmanager
    def connect():
        yield SimpleNamespace(scalar=lambda _: 1)

    monkeypatch.setattr(api_app, "get_engine", lambda: SimpleNamespace(connect=connect))
    monkeypatch.setattr(api_app, "cache_client", lambda: SimpleNamespace(ping=lambda: False))
    with TestClient(configure()) as client:
        response = client.get("/api/health/ready")
    assert response.status_code == 503
    assert response.json()["failed"] == ["redis"]


def test_import_app_does_not_read_settings(monkeypatch):
    from avelis import config

    def forbidden():
        raise AssertionError("导入模块不应读取配置")

    try:
        with monkeypatch.context() as isolated:
            isolated.setattr(config, "get_settings", forbidden)
            importlib.reload(api_app)
    finally:
        importlib.reload(api_app)


def test_module_asgi_app_loads_configuration_once(configure, monkeypatch):
    application = configure()
    calls = []

    def build():
        calls.append("create_app")
        return application

    monkeypatch.setattr(api_app, "create_app", build)
    api_app._application.cache_clear()
    try:
        with TestClient(api_app.app) as client:
            assert client.get("/api/health").json() == {"status": "ok"}
            assert client.get("/api/health").json() == {"status": "ok"}
        assert calls == ["create_app"]
    finally:
        api_app._application.cache_clear()


def test_invalid_frontend_stops_asgi_startup(monkeypatch, tmp_path):
    settings = build_settings(
        {"AVELIS_SECRET": "offline-unit-test-secret-0123456789", "AVELIS_FRONTEND_DIR": str(tmp_path)}
    )
    monkeypatch.setattr(api_app, "get_settings", lambda: settings)
    messages = []

    async def receive():
        messages.append("receive")
        return {"type": "lifespan.startup"}

    async def send(message):
        messages.append(message)

    api_app._application.cache_clear()
    try:
        with pytest.raises(ValueError, match="index.html"):
            asyncio.run(api_app.app({"type": "lifespan"}, receive, send))
        assert messages[0] == "receive"
        assert messages[1]["type"] == "lifespan.startup.failed"
        assert str(tmp_path) in messages[1]["message"]
    finally:
        api_app._application.cache_clear()


def test_api_paths_under_root_path_never_fall_back_to_frontend(configure, dist):
    with TestClient(configure(AVELIS_FRONTEND_DIR=str(dist)), root_path="/avelis") as client:
        assert client.get("/avelis/api/health").json() == {"status": "ok"}
        response = client.get("/avelis/api/not-exist")
        assert response.status_code == 404 and response.headers["content-type"].startswith("application/json")
        assert client.get("/avelis/lineage").headers["content-type"].startswith("text/html")
