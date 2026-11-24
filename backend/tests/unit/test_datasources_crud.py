"""Datasources CRUD (PATCH / DELETE / POST .../test) unit tests using in-memory SQLite."""

import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import JSON, MetaData, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from avelis.api import datasources
from avelis.api.deps import current_user, get_session
from avelis.db import Base
from avelis.engines import CredentialMissing
from avelis.governance.errors import GovernanceError
from avelis.models import (
    CollectionRun,
    Datasource,
    DatasourceCredential,
    EndpointAlias,
    User,
    UserRole,
)


@pytest.fixture
def ds_api(monkeypatch):
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
    metadata.create_all(engine)

    monkeypatch.setattr(datasources.audit, "record", lambda *args, **kwargs: None)
    app = FastAPI()
    app.include_router(datasources.router)

    @app.exception_handler(GovernanceError)
    async def governance_error(_: Request, exc: GovernanceError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message, **exc.detail})

    with Session(engine, expire_on_commit=False) as session:
        user = User(id="steward-1", username="steward", display_name="Steward", password_hash="unused")
        role = UserRole(user_id=user.id, role="steward")
        session.add_all([user, role])
        session.commit()
        app.dependency_overrides[current_user] = lambda: user
        app.dependency_overrides[get_session] = lambda: session
        with TestClient(app) as client:
            yield client, session, user, role
    engine.dispose()


def _create_pg(client: TestClient, name: str = "pg-prod") -> dict:
    res = client.post(
        "/api/datasources",
        json={
            "name": name,
            "kind": "postgres",
            "host": "10.0.0.10",
            "port": 5432,
            "options": {"database": "analytics", "schemas": "public"},
            "collect_interval_minutes": 15,
            "collect": {"username": "ro", "password": "secret"},
        },
    )
    assert res.status_code == 201, res.text
    return res.json()


def test_patch_datasource_updates_fields_and_validates(ds_api):
    client, session, _, role = ds_api
    ds1 = _create_pg(client, "pg-1")
    _create_pg(client, "pg-2")

    res = client.patch(
        f"/api/datasources/{ds1['id']}",
        json={
            "name": "pg-1-renamed",
            "host": "10.0.0.22",
            "port": 5433,
            "options": {"database": "dwh", "schemas": "analytics"},
            "collect_interval_minutes": 30,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["name"] == "pg-1-renamed"
    assert body["host"] == "10.0.0.22"
    assert body["port"] == 5433
    assert body["options"] == {"database": "dwh", "schemas": "analytics"}
    assert body["collect_interval_minutes"] == 30

    dup = client.patch(f"/api/datasources/{ds1['id']}", json={"name": "pg-2"})
    assert dup.status_code == 409

    missing_opt = client.patch(f"/api/datasources/{ds1['id']}", json={"options": {}})
    assert missing_opt.status_code == 422

    assert client.patch("/api/datasources/nonexistent", json={"name": "x"}).status_code == 404

    role.role = "analyst"
    session.commit()
    assert client.patch(f"/api/datasources/{ds1['id']}", json={"name": "nope"}).status_code == 403


def test_delete_datasource_cleans_uncollected_and_blocks_collected(ds_api):
    client, session, _, role = ds_api
    ds_clean = _create_pg(client, "pg-clean")
    alias_res = client.post(
        "/api/endpoint-aliases",
        json={"datasource_id": ds_clean["id"], "host": "127.0.0.1", "port": 15432, "note": "tunnel"},
    )
    assert alias_res.status_code == 201

    role.role = "analyst"
    session.commit()
    assert client.delete(f"/api/datasources/{ds_clean['id']}").status_code == 403

    role.role = "steward"
    session.commit()
    del_res = client.delete(f"/api/datasources/{ds_clean['id']}")
    assert del_res.status_code == 204
    assert session.get(Datasource, ds_clean["id"]) is None
    assert (
        session.scalars(select(DatasourceCredential).where(DatasourceCredential.datasource_id == ds_clean["id"])).all()
        == []
    )
    assert session.scalars(select(EndpointAlias).where(EndpointAlias.datasource_id == ds_clean["id"])).all() == []

    ds_used = _create_pg(client, "pg-used")
    session.add(CollectionRun(id="run-1", datasource_id=ds_used["id"], trigger="manual", status="succeeded"))
    session.commit()
    conflict = client.delete(f"/api/datasources/{ds_used['id']}")
    assert conflict.status_code == 409
    assert session.get(Datasource, ds_used["id"]) is not None


def test_test_datasource_connection_handles_success_missing_cred_error_and_timeout(ds_api, monkeypatch):
    client, session, _, _ = ds_api
    ds = _create_pg(client, "pg-test")

    monkeypatch.setattr(
        datasources,
        "collect_connector",
        lambda _s, _d: SimpleNamespace(server_identity=lambda: {"version": "16.2", "host": "10.0.0.10"}),
    )
    ok_res = client.post(f"/api/datasources/{ds['id']}/test")
    assert ok_res.status_code == 200
    assert ok_res.json()["ok"] is True
    assert ok_res.json()["server_identity"] == {"version": "16.2", "host": "10.0.0.10"}
    assert session.get(Datasource, ds["id"]).server_identity == {"version": "16.2", "host": "10.0.0.10"}

    def _boom(_s, _d):
        raise CredentialMissing("数据源 pg-test 未配置 collect 账号")

    monkeypatch.setattr(datasources, "collect_connector", _boom)
    miss_res = client.post(f"/api/datasources/{ds['id']}/test")
    assert miss_res.status_code == 200
    assert miss_res.json()["ok"] is False
    assert "pg-test" in miss_res.json()["error"]

    def _fail_identity():
        raise ConnectionRefusedError("Connection refused")

    monkeypatch.setattr(
        datasources, "collect_connector", lambda _s, _d: SimpleNamespace(server_identity=_fail_identity)
    )
    err_res = client.post(f"/api/datasources/{ds['id']}/test")
    assert err_res.status_code == 200
    assert err_res.json()["ok"] is False
    assert "Connection refused" in err_res.json()["error"]

    # Fast timeout simulation: patch FuturesTimeoutError handling via slow server_identity or small timeout
    class _FakeFuture:
        def result(self, timeout=None):
            raise datasources.FuturesTimeoutError()

        def cancel(self):
            return True

    class _FakePool:
        def __init__(self, max_workers=1):
            pass

        def submit(self, fn):
            return _FakeFuture()

        def shutdown(self, wait=False, cancel_futures=True):
            pass

    monkeypatch.setattr(
        datasources, "collect_connector", lambda _s, _d: SimpleNamespace(server_identity=lambda: time.sleep(0))
    )
    monkeypatch.setattr(datasources, "ThreadPoolExecutor", _FakePool)
    to_res = client.post(f"/api/datasources/{ds['id']}/test")
    assert to_res.status_code == 200
    assert to_res.json()["ok"] is False
    assert "8秒" in to_res.json()["error"]
