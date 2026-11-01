"""Worker 状态查询：使用 Redis 替身，无需登录。"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from avelis.api import app as api_app
from avelis.cache import WORKER_HEARTBEAT
from avelis.config import build_settings


@pytest.fixture
def app(monkeypatch):
    settings = build_settings({"AVELIS_SECRET": "offline-unit-test-secret-0123456789"})
    monkeypatch.setattr(api_app, "get_settings", lambda: settings)
    return api_app.create_app()


@pytest.mark.parametrize("last_seen", ["2026-11-01T02:03:04+00:00", "", None])
def test_worker_health_reports_heartbeat_without_login(app, monkeypatch, last_seen):
    def get(key):
        assert key == WORKER_HEARTBEAT
        return last_seen

    monkeypatch.setattr(api_app, "cache_client", lambda: SimpleNamespace(get=get))
    with TestClient(app) as client:
        response = client.get("/api/health/worker")
    assert response.status_code == 200
    assert response.json() == {"alive": last_seen is not None, "last_seen": last_seen}


@pytest.mark.parametrize("fail_at", ["client", "get"])
def test_worker_health_reports_unavailable_redis_as_inactive(app, monkeypatch, fail_at):
    def fail(*args):
        raise RuntimeError("Redis unavailable")

    factory = fail if fail_at == "client" else lambda: SimpleNamespace(get=fail)
    monkeypatch.setattr(api_app, "cache_client", factory)
    with TestClient(app) as client:
        response = client.get("/api/health/worker")
    assert response.status_code == 200
    assert response.json() == {"alive": False, "last_seen": None}
