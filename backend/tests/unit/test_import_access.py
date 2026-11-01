"""导入接口的读取权限：使用内存会话，不连接数据库。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from avelis.api import imports
from avelis.api.deps import current_user, get_session
from avelis.governance.errors import GovernanceError
from avelis.models import AppSetting, ImportBatch, ImportJob, ImportSource, User, UserRole


class ImportSession:
    def __init__(self, roles: set[str]):
        self.roles = roles
        self.reads = []
        self.batch = ImportBatch(
            id="batch-1",
            datasource_id="ds-1",
            status="done",
            progress={},
            stats={},
            options={},
            created_at=datetime(2026, 10, 29, tzinfo=UTC),
        )
        self.job = ImportJob(
            id="job-1",
            batch_id="batch-1",
            datasource_id="ds-1",
            filename="private.sql",
            language="sql",
            source_type="sql",
            status="done",
            params={"password": "private-param"},
            stats={},
            model_adapter="private-model",
            model_payload=[{"text": "private-model-payload"}],
            content_hash="digest",
            created_at=datetime(2026, 10, 29, tzinfo=UTC),
        )

    def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is UserRole:
            return self.roles
        self.reads.append(entity)
        if entity is ImportJob:
            return [self.job]
        assert entity is ImportBatch
        return [self.batch]

    def scalar(self, statement):
        assert statement.column_descriptions[0]["entity"] is ImportSource
        self.reads.append(ImportSource)
        return "SELECT 'private-source';"

    def get(self, entity, object_id):
        self.reads.append(entity)
        if entity is AppSetting:
            return None
        if entity is ImportBatch:
            assert object_id == self.batch.id
            return self.batch
        assert entity is ImportJob and object_id == self.job.id
        return self.job


def client_for(session: ImportSession) -> TestClient:
    app = FastAPI()
    app.include_router(imports.router)
    app.dependency_overrides[current_user] = lambda: User(id="reader")
    app.dependency_overrides[get_session] = lambda: session

    @app.exception_handler(GovernanceError)
    async def governance_error(_: Request, exc: GovernanceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message, **exc.detail})

    return TestClient(app)


@pytest.mark.parametrize("roles", [set(), {"analyst"}, {"reviewer"}, {"analyst", "reviewer"}])
@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/api/imports", None),
        ("GET", "/api/imports/batch-1", None),
        ("GET", "/api/imports/batch-1/statements", None),
        ("GET", "/api/imports/batch-1/candidates", None),
        ("GET", "/api/imports/batch-1/files/job-1/content", None),
        ("GET", "/api/imports/batch-1/model-payload", None),
        ("GET", "/api/imports/sql-functions/ds-1", None),
        ("PUT", "/api/imports/sql-functions/ds-1", {"sql_functions": ["run_sql"]}),
        ("POST", "/api/imports", {"datasource_id": "ds-1", "content": "SELECT 1"}),
        ("PUT", "/api/imports/batch-1/params", {"params": {"table": "events"}}),
        ("POST", "/api/candidates/pending-candidate/decision", {"decision": "accept", "reason": "确认"}),
        ("POST", "/api/candidates/decisions", {"ids": ["pending-candidate"], "decision": "reject", "reason": "复核"}),
        (
            "POST",
            "/api/imports/batch-1/edges",
            {
                "file_id": "job-1",
                "source_ref": "src.events",
                "target_ref": "dw.daily_summary",
                "relation": "table",
                "reason": "补充",
            },
        ),
    ],
)
def test_import_payload_routes_reject_before_reading(roles, method, path, body):
    session = ImportSession(roles)
    with client_for(session) as client:
        response = client.request(method, path, json=body)
    assert response.status_code == 403
    assert response.json()["required"] == ["steward"]
    assert session.reads == []
    assert "private-" not in response.text


@pytest.mark.parametrize("roles", [{"analyst"}, {"reviewer"}])
def test_upload_requires_import_access(roles):
    session = ImportSession(roles)
    with client_for(session) as client:
        response = client.post(
            "/api/imports/upload",
            data={"datasource_id": "ds-1"},
            files={"file": ("private.sql", b"SELECT 1", "text/plain")},
        )
    assert response.status_code == 403
    assert session.reads == []


@pytest.mark.parametrize("roles", [{"steward"}, {"admin"}, {"steward", "reviewer"}])
def test_steward_and_admin_can_list_and_read_import_payload(roles):
    session = ImportSession(roles)
    with client_for(session) as client:
        listed = client.get("/api/imports")
        original = client.get("/api/imports/batch-1/files/job-1/content")
        payloads = client.get("/api/imports/batch-1/model-payload")
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == "batch-1"
    assert original.status_code == 200
    assert original.json()["content"] == "SELECT 'private-source';"
    assert payloads.status_code == 200
    assert payloads.json() == [{"text": "private-model-payload", "file_id": "job-1", "filename": "private.sql"}]
