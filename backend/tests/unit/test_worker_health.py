"""Worker 状态查询：使用 Redis 替身，无需登录。"""

from datetime import timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from avelis import cache
from avelis.api import app as api_app
from avelis.config import build_settings


@pytest.fixture
def app(monkeypatch):
    settings = build_settings({"AVELIS_SECRET": "offline-unit-test-secret-0123456789"})
    monkeypatch.setattr(api_app, "get_settings", lambda: settings)
    return api_app.create_app()


@pytest.mark.parametrize("alive, last_seen", [(True, "2026-11-01T02:03:04+00:00"), (True, None), (False, None)])
def test_worker_health_reports_heartbeat_without_login(app, monkeypatch, alive, last_seen):
    def heartbeat_status(now):
        assert now.tzinfo is timezone.utc
        return alive, last_seen

    monkeypatch.setattr(api_app, "worker_heartbeat_status", heartbeat_status)
    with TestClient(app) as client:
        response = client.get("/api/health/worker")
    assert response.status_code == 200
    assert response.json() == {"alive": alive, "last_seen": last_seen}


@pytest.mark.parametrize("fail_at", ["client", "get"])
def test_worker_health_reports_unavailable_redis_as_inactive(app, monkeypatch, fail_at):
    fail = Mock(side_effect=RuntimeError("Redis unavailable"))
    connection = SimpleNamespace(zrevrangebyscore=Mock(return_value=[]), get=fail)
    factory = fail if fail_at == "client" else lambda: connection
    monkeypatch.setattr(cache, "client", factory)
    with TestClient(app) as client:
        response = client.get("/api/health/worker")
    fail.assert_called_once()
    assert response.status_code == 200
    assert response.json() == {"alive": False, "last_seen": None}
