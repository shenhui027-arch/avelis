"""用内存元数据库验证采集范围接口、任务入队和批次结果接线。"""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import JSON, MetaData, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from avelis.api import datasources, imports
from avelis.api.deps import current_user, get_session
from avelis.db import Base
from avelis.governance.errors import GovernanceError
from avelis.importer import batch
from avelis.importer.archive import ImportFile
from avelis.models import AuditEvent, Datasource, ImportBatch, Job, User, UserRole

DS_ID = "ds-1"
SCOPE_URL = f"/api/datasources/{DS_ID}/scope"


@pytest.fixture
def scope_api(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        execution_options={"schema_translate_map": {"avelis_agent": None}},
    )
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        copied = table.to_metadata(metadata)
        for column in copied.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
        for index in copied.indexes:
            if index.name == "ix_import_job_current_source":
                # 同一来源可以保留多个历史版本，唯一约束只作用于当前版本。
                index.dialect_options["sqlite"]["where"] = index.dialect_options["postgresql"]["where"]
    metadata.create_all(engine)
    app = FastAPI()
    app.include_router(datasources.router)
    app.include_router(imports.router)

    @app.exception_handler(GovernanceError)
    async def governance_error(_: Request, exc: GovernanceError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message, **exc.detail})

    with Session(engine, expire_on_commit=False) as session:
        user = User(id="steward-1", username="steward", display_name="维护者", password_hash="unused")
        role = UserRole(user_id=user.id, role="steward")
        ds = Datasource(
            id=DS_ID,
            name="pg-test",
            kind="postgres",
            host="localhost",
            port=5432,
            options={"database": "analytics", "schemas": "public, reporting"},
        )
        session.add_all([user, role, ds])
        session.commit()
        app.dependency_overrides[current_user] = lambda: user
        app.dependency_overrides[get_session] = lambda: session

        @contextmanager
        def job_session():
            # 任务使用独立事务，入队时范围和审计必须已经提交。
            assert not session.in_transaction()
            with Session(engine) as queued, queued.begin():
                yield queued

        monkeypatch.setattr(datasources.jobs, "session_scope", job_session)
        monkeypatch.setattr(datasources.jobs, "_wake", lambda _job_id: None)
        with TestClient(app) as client:
            yield client, session, user, role
    engine.dispose()


def test_scope_add_requires_steward(scope_api):
    client, session, _, role = scope_api
    role.role = "analyst"
    session.commit()
    assert client.post(SCOPE_URL, json={"names": ["ods"]}).status_code == 403


def test_scope_add_returns_404_for_missing_datasource(scope_api):
    client, _, _, _ = scope_api
    assert client.post("/api/datasources/missing/scope", json={"names": ["ods"]}).status_code == 404


@pytest.mark.parametrize("names", [[], ["ods", " "], [f"db{i}" for i in range(21)]])
def test_scope_add_rejects_invalid_names(scope_api, names):
    client, _, _, _ = scope_api
    assert client.post(SCOPE_URL, json={"names": names}).status_code == 422


def test_scope_add_rejects_invalid_collector_name_without_side_effects(scope_api):
    client, session, _, _ = scope_api
    ds = session.get(Datasource, DS_ID)
    original = ds.options.copy()

    response = client.post(SCOPE_URL, json={"names": ["ods", "bad-schema"]})

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "库或模式名 bad-schema 不符合采集器的命名规则"
    assert session.scalars(select(AuditEvent)).all() == []
    assert session.scalars(select(Job)).all() == []
    session.refresh(ds)
    assert ds.options == original


def test_scope_add_rejects_engine_without_scope_key(scope_api):
    client, session, _, _ = scope_api
    ds = session.get(Datasource, DS_ID)
    ds.kind = "fake"
    ds.is_test_adapter = True
    ds.options = {}
    session.commit()
    assert client.post(SCOPE_URL, json={"names": ["ods"]}).status_code == 422


def test_scope_add_persists_options_audits_and_enqueues_collection(scope_api):
    client, session, user, _ = scope_api
    response = client.post(SCOPE_URL, json={"names": ["ods", "PUBLIC", "ODS", "warehouse"]})
    assert response.status_code == 200, response.text
    (job,) = session.scalars(select(Job)).all()
    assert response.json() == {"added": ["ods", "warehouse"], "job_id": job.id}
    assert (job.kind, job.status, job.created_by) == ("collect", "queued", user.id)
    assert job.payload["datasource_id"] == DS_ID
    assert job.payload["trigger"] == "manual"

    session.expire_all()
    assert session.get(Datasource, DS_ID).options == {
        "database": "analytics",
        "schemas": ["public", "reporting", "ods", "warehouse"],
    }
    (event,) = session.scalars(select(AuditEvent)).all()
    assert (event.action, event.object_type, event.object_id, event.actor_id) == (
        "datasource.scope_add",
        "datasource",
        DS_ID,
        user.id,
    )
    assert event.detail["key"] == "schemas"
    assert event.detail["added"] == ["ods", "warehouse"]


def test_scope_add_existing_names_skips_collection_and_audit(scope_api):
    client, session, _, _ = scope_api
    response = client.post(SCOPE_URL, json={"names": ["PUBLIC", "reporting", "public"]})
    assert response.status_code == 200, response.text
    assert response.json() == {"added": [], "job_id": None}
    assert session.scalars(select(Job)).all() == []
    assert session.scalars(select(AuditEvent)).all() == []
    assert session.get(Datasource, DS_ID).options["schemas"] == "public, reporting"


def test_batch_view_annotates_unmatched_using_current_scope(scope_api):
    client, session, user, _ = scope_api
    source = {"ref": "analytics.ods.events", "role": "source", "count": 2, "statement_ids": ["s1"]}
    target = {"ref": "analytics.dw.summary", "role": "target", "count": 1, "statement_ids": ["s1"]}
    imported = ImportBatch(
        datasource_id=DS_ID,
        status="done",
        created_by=user.id,
        stats={"unmatched_tables": [source, target]},
    )
    session.add(imported)
    session.commit()

    assert batch.batch_view(session, imported)["unmatched_tables"] == [
        {**source, "database": "ods", "in_scope": False, "addable": True},
        {**target, "database": "dw"},
    ]
    response = client.post(SCOPE_URL, json={"names": ["ODS"]})
    assert response.status_code == 200, response.text
    session.expire_all()
    assert batch.batch_view(session, imported)["unmatched_tables"] == [
        {**source, "database": "ods", "in_scope": True, "addable": False},
        {**target, "database": "dw"},
    ]
    assert imported.stats["unmatched_tables"] == [source, target]


def test_batch_view_marks_external_catalog_and_invalid_collector_name(scope_api):
    _, session, user, _ = scope_api
    ds = session.get(Datasource, DS_ID)
    ds.kind = "starrocks"
    ds.options = {"databases": ["ods"]}
    external = {"ref": "jdbc_catalog.ods.events", "role": "source", "count": 1, "statement_ids": ["s1"]}
    invalid = {"ref": "default_catalog.bad db.events", "role": "source", "count": 1, "statement_ids": ["s1"]}
    internal = {"ref": "DEFAULT_CATALOG.ods-log.events", "role": "source", "count": 1, "statement_ids": ["s1"]}
    imported = ImportBatch(
        datasource_id=DS_ID,
        status="done",
        created_by=user.id,
        stats={"unmatched_tables": [external, invalid, internal]},
    )
    session.add(imported)
    session.commit()

    assert batch.batch_view(session, imported)["unmatched_tables"] == [
        {**external, "database": "ods", "in_scope": False, "addable": False, "external_catalog": True},
        {**invalid, "database": "bad db", "in_scope": False, "addable": False, "invalid_name": True},
        {**internal, "database": "ods-log", "in_scope": False, "addable": True},
    ]


@pytest.mark.parametrize(
    ("params", "note"),
    [({}, "整批重新解析"), ({"schema": "reporting"}, "补充参数后整批重新解析")],
)
def test_params_reparse_note_describes_submitted_changes(scope_api, monkeypatch, params, note):
    client, session, user, _ = scope_api
    settings = SimpleNamespace(import_max_bytes=1024, import_max_batch_bytes=1024)
    monkeypatch.setattr(batch, "get_settings", lambda: settings)
    imported, _ = batch.create_batch(
        session,
        user,
        datasource_id=DS_ID,
        files=[ImportFile("source.sql", "SELECT id FROM public.events", "sql")],
        params={"schema": "public"},
    )
    imported.status = "done"
    session.commit()

    response = client.put(f"/api/imports/{imported.id}/params", json={"params": params})

    assert response.status_code == 202, response.text
    assert response.json()["notes"] == [note]
    assert response.json()["params"] == {"schema": "public", **params}


def test_valid_name_uses_the_checker_the_collector_actually_calls():
    """MySQL 采集器从 StarRocks 模块导入 _checked（允许连字符），不能退回保守规则误判。"""
    from avelis.engines import get_engine
    from avelis.importer.scope import valid_name

    mysql = get_engine("mysql")
    assert valid_name(mysql, "sales-archive") is True
    assert valid_name(mysql, "bad name") is False
    postgres = get_engine("postgres")
    assert valid_name(postgres, "sales-archive") is False
