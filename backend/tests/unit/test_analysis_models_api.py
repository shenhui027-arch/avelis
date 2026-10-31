"""分析模型 HTTP 合约及 ORM 持久化；使用内存 SQLite，不连接模型或外部服务。"""

import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import JSON, MetaData, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from avelis import modelcfg
from avelis.analysis import service
from avelis.api import analysis, model_settings
from avelis.api.deps import current_user, get_session
from avelis.db import Base
from avelis.governance.errors import GovernanceError
from avelis.models import (
    AnalysisDataset, AnalysisMessage, AnalysisRun, AnalysisStep, AnalysisTask, AppSetting,
    Job, ModelAssignment, ModelProvider, User, UserRole,
)


@pytest.fixture
def api(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in (User, UserRole, ModelProvider, ModelAssignment, AppSetting, AnalysisTask,
                  AnalysisRun, AnalysisStep, AnalysisDataset, AnalysisMessage, Job):
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(engine)
    calls = []

    def build(provider, model_id, **kwargs):
        calls.append((provider.id, model_id, kwargs))
        return SimpleNamespace()

    monkeypatch.setattr(modelcfg, "build_chat_model", build)
    monkeypatch.setattr(modelcfg, "decrypt_secret", lambda _: "offline-test-key")
    monkeypatch.setattr(modelcfg.audit, "record", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "warm_up", lambda: None)
    monkeypatch.setattr(analysis.jobs, "_wake", lambda _: None)
    app = FastAPI()
    app.include_router(analysis.router)
    app.include_router(model_settings.router)

    @app.exception_handler(GovernanceError)
    async def governance_error(_: Request, exc: GovernanceError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message, **exc.detail})

    with Session(engine, expire_on_commit=False) as session:
        user = User(id="actor", username="actor", display_name="分析用户", password_hash="unused")
        session.add_all([user, UserRole(user_id=user.id, role="analyst")])
        provider = ModelProvider(id="ds", code="ds", name="DeepSeek", kind="openai_compatible", preset="deepseek",
                                 base_url="https://api.deepseek.com", api_key_enc="offline-ciphertext",
                                 api_key_hint="hint", created_by=user.id,
                                 models=[{"id": "chosen", "enabled": True, "context_window": 131072},
                                         {"id": "default", "enabled": True}],
                                 last_check={"models": {mid: {"ok": True, "tool_calling": True}
                                                        for mid in ("chosen", "default")}})
        session.add(provider)
        session.add(ModelAssignment(purpose="ask", provider_id=provider.id, model_id="default",
                                    reasoning_effort="high", updated_by=user.id))
        session.commit()
        app.dependency_overrides[current_user] = lambda: user
        app.dependency_overrides[get_session] = lambda: session
        with TestClient(app) as client:
            yield client, session, user, provider, calls
    engine.dispose()


@pytest.mark.parametrize("role,expected", [("analyst", 200), ("steward", 200), ("reviewer", 200),
                                           ("admin", 200), ("viewer", 403)])
def test_models_http_roles_and_secret_free_response(api, role, expected):
    client, session, _, _, calls = api
    session.scalar(select(UserRole)).role = role
    session.commit()
    response = client.get("/api/analysis/models")
    assert response.status_code == expected, response.text
    if expected == 200:
        data = response.json()
        assert data["default"] == {"provider_id": "ds", "model_id": "default", "reasoning_effort": "high"}
        assert data["models"][0] == {
            "provider_id": "ds", "provider_name": "DeepSeek", "model_id": "chosen", "context_window": 131072,
            "efforts": [{"value": value, "label": label}
                        for value, label in (("off", "关闭"), ("low", "低"), ("high", "高"), ("max", "最高"))],
        }
        assert data["models"][1]["context_window"] is None
        assert all(secret not in response.text for secret in ("offline-ciphertext", "offline-test-key", "hint", "api_key"))
    assert calls == []


def test_run_and_followup_http_persist_choice_and_keep_default_unselected(api):
    client, session, _, _, calls = api
    selected = {"provider_id": "ds", "model_id": "chosen"}
    choice = {**selected, "reasoning_effort": "max"}
    response = client.post("/api/analysis/runs", json={"question": "检查指标变化", "model": selected,
                                                       "reasoning_effort": "max"})
    assert response.status_code == 201, response.text
    assert response.json()["model_choice"] == choice
    run_id = response.json()["id"]
    session.expunge_all()
    parent = session.get(AnalysisRun, run_id)
    assert parent.model_choice == choice
    assert calls[-1][:2] == ("ds", "chosen") and calls[-1][2]["effort"] == "max"
    parent.status, parent.reasoning_effort = "succeeded", "max"
    session.commit()
    followup = client.post(f"/api/analysis/runs/{run_id}/followup", json={"text": "继续分析"})
    assert followup.status_code == 201, followup.text
    assert followup.json()["model_choice"] == choice
    assert client.get(f"/api/analysis/runs/{run_id}").json()["reasoning_effort"] == "max"
    assert all(row["model_choice"] == choice for row in client.get("/api/analysis/runs").json())
    response = client.post("/api/analysis/runs", json={"question": "默认模型分析"})
    assert response.status_code == 201, response.text
    assert response.json()["model_choice"] is None
    assert calls[-1][:2] == ("ds", "default") and calls[-1][2]["effort"] == "high"


@pytest.mark.parametrize("selection", [
    {"model": {"provider_id": "missing", "model_id": "chosen"}},
    {"model": {"provider_id": "ds", "model_id": "missing"}},
    {"model": {"provider_id": "ds", "model_id": "chosen"}, "reasoning_effort": "medium"},
    {"reasoning_effort": "unknown"},
])
def test_invalid_model_or_effort_returns_http_400_for_create_and_followup(api, selection):
    client, session, _, _, calls = api
    created = client.post("/api/analysis/runs", json={"question": "默认模型分析"})
    parent = session.get(AnalysisRun, created.json()["id"])
    parent.status = "succeeded"
    session.commit()
    calls.clear()
    for path, text in (("/api/analysis/runs", {"question": "检查指标变化"}),
                       (f"/api/analysis/runs/{parent.id}/followup", {"text": "继续分析"})):
        response = client.post(path, json={**text, **selection})
        assert response.status_code == 400, response.text
    assert calls == []
    assert len(list(session.scalars(select(AnalysisRun)))) == 1


@pytest.mark.parametrize("context_window", [1024, 10_000_000, None])
def test_provider_http_saves_and_returns_context_window(api, context_window):
    client, session, _, _, _ = api
    session.scalar(select(UserRole)).role = "admin"
    session.commit()
    response = client.post("/api/model-providers", json={"code": "context", "preset": "deepseek",
        "models": [{"id": "custom-model", "context_window": context_window}]})
    assert response.status_code == 201, response.text
    provider_id = response.json()["id"]
    assert response.json()["models"][0]["context_window"] == context_window
    session.expunge_all()
    assert session.get(ModelProvider, provider_id).models[0]["context_window"] == context_window
    response = client.patch(f"/api/model-providers/{provider_id}", json={
        "models": [{"id": "custom-model", "context_window": 131072}]})
    assert response.status_code == 200, response.text
    assert response.json()["models"][0]["context_window"] == 131072


@pytest.mark.parametrize("context_window", [1023, 10_000_001, 1024.5, True, "131072"])
def test_provider_http_rejects_invalid_context_window(api, context_window):
    client, session, _, _, _ = api
    session.scalar(select(UserRole)).role = "admin"
    session.commit()
    response = client.patch("/api/model-providers/ds", json={
        "models": [{"id": "chosen", "context_window": context_window}]})
    assert response.status_code == 422, response.text
    session.expire_all()
    assert session.get(ModelProvider, "ds").models[0]["context_window"] == 131072


def test_model_choice_migration_compiles_postgresql_upgrade_and_downgrade():
    path = Path(__file__).resolve().parents[2] / "src/avelis/migrations/versions/0013_analysis_model_choice.py"
    spec = importlib.util.spec_from_file_location("migration_0013", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "0012"
    ddl = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={
        "as_sql": True, "output_buffer": ddl, "target_metadata": Base.metadata})
    with Operations.context(context):
        migration.upgrade()
        migration.downgrade()
    assert "ADD COLUMN model_choice JSONB" in ddl.getvalue()
    assert "DROP COLUMN model_choice" in ddl.getvalue()
    assert AnalysisRun.__table__.c.model_choice.nullable is True
