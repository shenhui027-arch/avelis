"""定时表达式预览 HTTP 合约；只用内存 SQLite 验证权限，不连接外部服务。"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from avelis.analysis import service
from avelis.api import analysis
from avelis.api.deps import current_user, get_session
from avelis.governance.errors import GovernanceError
from avelis.models import User, UserRole

NOW = datetime(2026, 11, 1, 23, 30, 45)
PATH = "/api/analysis/cron/preview"


@pytest.fixture
def api(monkeypatch):
    settings = SimpleNamespace(business_timezone="UTC")
    monkeypatch.setattr(analysis, "get_settings", lambda: settings)
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    monkeypatch.setattr(analysis, "utcnow", lambda: NOW)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in (User, UserRole):
        model.__table__.to_metadata(metadata)
    metadata.create_all(engine)
    app = FastAPI()
    app.include_router(analysis.router)

    @app.exception_handler(GovernanceError)
    async def governance_error(_: Request, exc: GovernanceError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message, **exc.detail})

    with Session(engine, expire_on_commit=False) as session:
        user = User(id="actor", username="actor", display_name="分析用户", password_hash="unused")
        session.add_all([user, UserRole(user_id=user.id, role="analyst")])
        session.commit()
        app.dependency_overrides[current_user] = lambda: user
        app.dependency_overrides[get_session] = lambda: session
        with TestClient(app) as client:
            yield client, session, settings
    engine.dispose()


def test_preview_returns_increasing_times_matching_cron(api):
    client, _, _ = api
    expr = "5,35 8-10/2 * 11 1-5"
    response = client.get(PATH, params={"expr": expr, "count": 6})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["expr"] == expr and data["timezone"] == "UTC" and data["error"] is None
    runs = [datetime.fromisoformat(value) for value in data["runs"]]
    assert len(runs) == 6
    assert runs[0] == datetime(2026, 11, 2, 8, 5, tzinfo=timezone.utc)
    assert all(before < after for before, after in zip([NOW.replace(tzinfo=timezone.utc), *runs], runs))
    assert all(
        run.month == 11
        and run.weekday() < 5
        and run.hour in {8, 10}
        and run.minute in {5, 35}
        and run.second == 0
        and run.microsecond == 0
        and run.utcoffset() == timedelta(0)
        for run in runs
    )


@pytest.mark.parametrize("count,expected", [(None, 5), (1, 1), (10, 10)])
def test_preview_default_count_and_inclusive_limits(api, count, expected):
    client, _, _ = api
    params = {"expr": "* * * * *"}
    if count is not None:
        params["count"] = count
    response = client.get(PATH, params=params)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["error"] is None
    runs = [datetime.fromisoformat(value) for value in data["runs"]]
    assert runs == [datetime(2026, 11, 1, 23, 31, tzinfo=timezone.utc) + timedelta(minutes=i) for i in range(expected)]


@pytest.mark.parametrize("timezone_name,offset", [("UTC", 0), ("Asia/Shanghai", 8)])
def test_preview_uses_business_timezone_across_utc_date_boundary(api, timezone_name, offset):
    client, _, settings = api
    settings.business_timezone = timezone_name
    response = client.get(PATH, params={"expr": "0 9 * * *", "count": 3})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["timezone"] == timezone_name and data["error"] is None
    expected_offset = timezone(timedelta(hours=offset))
    assert data["runs"] == [datetime(2026, 11, day, 9, tzinfo=expected_offset).isoformat() for day in (2, 3, 4)]


@pytest.mark.parametrize(
    "expr,error",
    [
        ("", "cron 需要 5 段：分 时 日 月 周（如 0 9 1 * * 表示每月 1 日 9:00）"),
        ("* * * *", "cron 需要 5 段：分 时 日 月 周（如 0 9 1 * * 表示每月 1 日 9:00）"),
        ("60 9 * * *", "cron 表达式无效：60 9 * * *"),
        ("0 9 30 2 *", "cron 表达式不会触发（日期不存在，如 2 月 30 日）：0 9 30 2 *"),
    ],
)
def test_preview_invalid_or_impossible_cron_is_successful_response_with_error(api, expr, error):
    client, _, _ = api
    response = client.get(PATH, params={"expr": expr})
    assert response.status_code == 200, response.text
    assert response.json() == {"expr": expr, "timezone": "UTC", "runs": [], "error": error}


@pytest.mark.parametrize("count", [0, 11])
def test_preview_rejects_out_of_range_count(api, count):
    client, _, _ = api
    response = client.get(PATH, params={"expr": "* * * * *", "count": count})
    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"] == ["query", "count"]


@pytest.mark.parametrize(
    "role,expected", [("analyst", 200), ("steward", 200), ("reviewer", 200), ("admin", 200), ("viewer", 403)]
)
def test_preview_requires_analysis_task_creation_role(api, role, expected):
    client, session, _ = api
    session.scalar(select(UserRole)).role = role
    session.commit()
    response = client.get(PATH, params={"expr": "0 9 * * *"})
    assert response.status_code == expected, response.text


def test_preview_openapi_exports_response_and_count_constraints(api):
    client, _, _ = api
    document = client.get("/openapi.json").json()
    operation = document["paths"][PATH]["get"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/CronPreviewOut"
    }
    schema = document["components"]["schemas"]["CronPreviewOut"]["properties"]
    assert set(schema) == {"expr", "timezone", "runs", "error"}
    assert schema["runs"]["type"] == "array" and schema["runs"]["items"]["type"] == "string"
    params = {parameter["name"]: parameter for parameter in operation["parameters"]}
    assert params["expr"]["required"] is True
    assert params["count"]["schema"]["default"] == 5
    assert params["count"]["schema"]["minimum"] == 1
    assert params["count"]["schema"]["maximum"] == 10
