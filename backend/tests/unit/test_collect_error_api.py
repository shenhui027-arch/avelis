"""采集批次和测试连接的错误响应及请求语言。"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from avelis.api import datasources
from avelis.api.deps import current_user, get_session
from avelis.collect.service import CollectionFailed
from avelis.i18n import LocaleMiddleware, translate
from avelis.models import CollectionRun, Datasource, User

DRIVER_ERROR = "Connection refused"
UNREACHABLE_ZH_CN = "无法连接到数据库：请检查地址、端口和网络是否可达"


def _failed_run(error: str) -> CollectionRun:
    return CollectionRun(
        id="run-1",
        datasource_id="ds-1",
        trigger="manual",
        status="failed",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        stats={},
        error=error,
    )


@pytest.mark.parametrize(
    ("error", "kind", "message"),
    [
        (DRIVER_ERROR, "unreachable", UNREACHABLE_ZH_CN),
        ("  KeyError: 'TABLE_SCHEMA'\n", "unknown", None),
    ],
)
def test_run_view_error(error, kind, message):
    run = _failed_run(error)
    view = datasources.run_view(run)
    assert view["error_kind"] == kind
    assert view["error_message"] == message
    assert view["error"] == run.error == error


@pytest.fixture
def client(monkeypatch):
    session = Mock(spec=Session)
    session.get.return_value = Datasource(id="ds-1")
    session.scalars.return_value = [_failed_run(DRIVER_ERROR)]
    monkeypatch.setattr(datasources.access, "require_role", lambda *args: None)
    monkeypatch.setattr(datasources.audit, "record", lambda *args, **kwargs: None)

    app = FastAPI()
    app.add_middleware(LocaleMiddleware)
    app.include_router(datasources.router)
    app.dependency_overrides[current_user] = lambda: User(id="steward-1")
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.parametrize(
    ("language", "message"),
    [
        ("zh-CN", UNREACHABLE_ZH_CN),
        ("zh-TW", "無法連線到資料庫：請檢查位址、連接埠和網路是否可達"),
        ("en", "Unable to connect to the database. Check the address, port, and network connectivity."),
    ],
)
def test_error_responses_follow_request_language(client, monkeypatch, language, message):
    collector = SimpleNamespace(server_identity=Mock(side_effect=ConnectionRefusedError(DRIVER_ERROR)))
    monkeypatch.setattr(datasources, "collect_connector", lambda *_: collector)
    headers = {"Accept-Language": language}

    test_response = client.post("/api/datasources/ds-1/test", headers=headers)
    assert test_response.status_code == 200
    connection = test_response.json()
    assert connection["ok"] is False
    assert connection["error"] == DRIVER_ERROR
    assert connection["error_kind"] == "unreachable"
    assert connection["error_message"] == message

    runs_response = client.get("/api/datasources/ds-1/runs", headers=headers)
    assert runs_response.status_code == 200
    [run] = runs_response.json()
    assert run["error"] == DRIVER_ERROR
    assert run["error_kind"] == "unreachable"
    assert run["error_message"] == message


def test_successful_connection_has_null_error_fields(client, monkeypatch):
    collector = SimpleNamespace(server_identity=lambda: {"version": "16.2"})
    monkeypatch.setattr(datasources, "collect_connector", lambda *_: collector)

    response = client.post("/api/datasources/ds-1/test")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["error"] is None
    assert body["error_kind"] is None
    assert body["error_message"] is None


def test_wait_collect_failure_detail_uses_explanation(monkeypatch):
    session = Mock(spec=Session)
    session.get.return_value = Datasource(id="ds-1")
    monkeypatch.setattr(datasources.access, "require_role", lambda *args: None)

    def fail(*_args, **_kwargs):
        raise CollectionFailed(DRIVER_ERROR)

    monkeypatch.setattr(datasources.jobs, "collect_now", fail)
    with pytest.raises(HTTPException) as caught:
        datasources.collect("ds-1", wait=True, user=User(id="steward-1"), session=session)
    assert caught.value.status_code == 502
    assert translate(caught.value.detail) == f"采集失败：{UNREACHABLE_ZH_CN}"

    # 认不出的错误照旧带原文
    monkeypatch.setattr(
        datasources.jobs, "collect_now", lambda *_a, **_k: (_ for _ in ()).throw(CollectionFailed("boom"))
    )
    with pytest.raises(HTTPException) as caught:
        datasources.collect("ds-1", wait=True, user=User(id="steward-1"), session=session)
    assert translate(caught.value.detail) == "采集失败：boom"
