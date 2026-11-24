"""Scheduled analysis tasks CRUD + options persistence + migration 0028 unit tests."""

import importlib.util
import io
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import JSON, Column, DateTime, MetaData, String, Table, Text, create_engine, inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from avelis import modelcfg
from avelis.analysis import service
from avelis.api import analysis
from avelis.api.deps import current_user, get_session
from avelis.governance.errors import GovernanceError, Invalid
from avelis.models import (
    AnalysisDataset,
    AnalysisMessage,
    AnalysisRun,
    AnalysisStep,
    AnalysisTask,
    AppSetting,
    AuditEvent,
    Job,
    ModelAssignment,
    ModelProvider,
    Notification,
    Skill,
    SkillEnablement,
    SkillVersion,
    User,
    UserRole,
)


def _load_migration():
    path = Path(__file__).resolve().parents[2] / "src/avelis/migrations/versions/0028_analysis_task_options.py"
    spec = importlib.util.spec_from_file_location("migration_0028", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_migration_0028_ddl_and_model_parity(monkeypatch):
    migration = _load_migration()
    assert migration.revision == "0028_analysis_task_options"
    assert migration.down_revision == "0027_auth_extensions"

    ddl = io.StringIO()
    ctx = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": ddl})
    with Operations.context(ctx):
        migration.upgrade()
        migration.downgrade()
    sql = ddl.getvalue()
    assert "ALTER TABLE analysis_task ADD COLUMN skills JSONB" in sql
    assert "ALTER TABLE analysis_task ADD COLUMN model_choice JSONB" in sql
    assert "ALTER TABLE analysis_task ADD COLUMN reasoning_effort VARCHAR(8)" in sql
    assert "ALTER TABLE analysis_task DROP COLUMN reasoning_effort" in sql

    monkeypatch.setattr(migration, "JsonType", JSON())
    engine = create_engine("sqlite://")
    metadata = MetaData()
    Table(
        "app_user",
        metadata,
        Column("id", String(32), primary_key=True),
    )
    Table(
        "analysis_task",
        metadata,
        Column("id", String(32), primary_key=True),
        Column("title", String(255), nullable=False),
        Column("question", Text, nullable=False),
        Column("owner_id", String(32), nullable=False),
        Column("window", JSON, nullable=False),
        Column("schedule_cron", String(64)),
        Column("status", String(16), nullable=False),
        Column("paused_reason", Text),
        Column("next_run_at", DateTime),
        Column("last_run_id", String(32)),
        Column("created_at", DateTime, nullable=False),
        Column("updated_at", DateTime, nullable=False),
    )
    metadata.create_all(engine)
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        inspector = inspect(conn)
        cols = {c["name"]: c for c in inspector.get_columns("analysis_task")}
        assert set(cols) == set(AnalysisTask.__table__.c.keys())
        for col in AnalysisTask.__table__.c:
            assert cols[col.name]["nullable"] == col.nullable
    engine.dispose()


@pytest.fixture
def task_api(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in (
        User,
        UserRole,
        ModelProvider,
        ModelAssignment,
        AppSetting,
        Skill,
        SkillVersion,
        SkillEnablement,
        AnalysisTask,
        AnalysisRun,
        AnalysisStep,
        AnalysisDataset,
        AnalysisMessage,
        Notification,
        Job,
        AuditEvent,
    ):
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(engine)

    monkeypatch.setattr(modelcfg, "build_chat_model", lambda *_a, **_kw: SimpleNamespace())
    monkeypatch.setattr(modelcfg, "decrypt_secret", lambda _: "offline-key")
    monkeypatch.setattr(service.audit, "record", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        service.skill_service,
        "enabled_catalog",
        lambda _s, _u, names=None: [{"name": "cohort-retention"}] if not names or "cohort-retention" in names else [],
    )

    app = FastAPI()
    app.include_router(analysis.router)

    @app.exception_handler(GovernanceError)
    async def governance_error(_: Request, exc: GovernanceError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message, **exc.detail})

    with Session(engine, expire_on_commit=False) as session:
        user = User(id="analyst-1", username="analyst", display_name="Analyst", password_hash="unused")
        other = User(id="analyst-2", username="other", display_name="Other", password_hash="unused")
        session.add_all(
            [
                user,
                other,
                UserRole(user_id=user.id, role="analyst"),
                UserRole(user_id=other.id, role="analyst"),
                ModelProvider(
                    id="ds",
                    code="ds",
                    name="DeepSeek",
                    kind="openai_compatible",
                    preset="deepseek",
                    base_url="https://api.deepseek.com",
                    api_key_enc="enc",
                    api_key_hint="hint",
                    created_by=user.id,
                    models=[{"id": "deepseek-reasoner", "enabled": True}, {"id": "deepseek-chat", "enabled": True}],
                    last_check={
                        "models": {
                            "deepseek-reasoner": {"ok": True, "tool_calling": True},
                            "deepseek-chat": {"ok": True, "tool_calling": True},
                        }
                    },
                ),
            ]
        )
        session.commit()
        app.dependency_overrides[current_user] = lambda: user
        app.dependency_overrides[get_session] = lambda: session
        with TestClient(app) as client:
            yield client, session, user, other, engine
    engine.dispose()


def test_task_crud_persists_model_skills_and_detaches_runs_on_delete(task_api):
    client, session, user, other, _ = task_api

    create_res = client.post(
        "/api/analysis/tasks",
        json={
            "title": "每周留存复盘",
            "question": "分析近30天各渠道留存变化",
            "window": {"kind": "last_month"},
            "schedule_cron": "0 9 * * 1",
            "skills": ["cohort-retention"],
            "model": {"provider_id": "ds", "model_id": "deepseek-reasoner"},
            "reasoning_effort": "high",
        },
    )
    assert create_res.status_code == 201, create_res.text
    task = create_res.json()
    assert task["title"] == "每周留存复盘"
    assert task["skills"] == ["cohort-retention"]
    assert task["model_choice"] == {"provider_id": "ds", "model_id": "deepseek-reasoner", "reasoning_effort": "high"}
    assert task["reasoning_effort"] == "high"

    bad_skill = client.post(
        "/api/analysis/tasks",
        json={"question": "测试未启用技能", "skills": ["disabled-skill"]},
    )
    assert bad_skill.status_code == 422

    patch_res = client.patch(
        f"/api/analysis/tasks/{task['id']}",
        json={
            "title": "每周留存复盘 V2",
            "question": "分析近60天各渠道留存与复购",
            "schedule_cron": "30 10 * * 1",
            "skills": [],
            "model": {"provider_id": "ds", "model_id": "deepseek-chat"},
            "reasoning_effort": "low",
        },
    )
    assert patch_res.status_code == 200, patch_res.text
    updated = patch_res.json()
    assert updated["title"] == "每周留存复盘 V2"
    assert updated["question"] == "分析近60天各渠道留存与复购"
    assert updated["skills"] == []
    assert updated["model_choice"] == {"provider_id": "ds", "model_id": "deepseek-chat", "reasoning_effort": "low"}

    # Attach an AnalysisRun and verify delete detaches task_id rather than deleting the run
    run = AnalysisRun(
        id="run-attached-1",
        task_id=task["id"],
        conversation_id="run-attached-1",
        user_id=user.id,
        question=updated["question"],
        trigger="schedule",
        status="succeeded",
        created_at=datetime.now(timezone.utc),
    )
    session.add(run)
    session.commit()

    del_res = client.delete(f"/api/analysis/tasks/{task['id']}")
    assert del_res.status_code == 204
    assert session.get(AnalysisTask, task["id"]) is None
    session.expire_all()
    preserved = session.get(AnalysisRun, "run-attached-1")
    assert preserved is not None
    assert preserved.task_id is None


def test_run_task_forwards_saved_model_skills_and_effort(task_api, monkeypatch):
    client, session, user, _, engine = task_api
    create_res = client.post(
        "/api/analysis/tasks",
        json={
            "title": "模型技能透传",
            "question": "华东区GMV走势",
            "skills": ["cohort-retention"],
            "model": {"provider_id": "ds", "model_id": "deepseek-reasoner"},
            "reasoning_effort": "high",
        },
    )
    assert create_res.status_code == 201
    task_id = create_res.json()["id"]

    job = Job(
        id="job-task-1", kind="analysis", payload={"task_id": task_id}, status="running", attempts=1, created_by=user.id
    )
    session.add(job)
    session.commit()

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(service, "session_scope", lambda: _ctx(factory))
    monkeypatch.setattr(service, "warm_up", lambda: None)

    captured = {}

    def fake_create_run(s, owner, question, window, **kwargs):
        captured["skills"] = kwargs.get("skills")
        captured["_model_choice"] = kwargs.get("_model_choice")
        captured["reasoning_effort"] = kwargs.get("reasoning_effort")
        return SimpleNamespace(id="fake-run-1")

    monkeypatch.setattr(service, "create_run", fake_create_run)
    monkeypatch.setattr(
        service, "execute", lambda _s, run_id, _j, _a, finish=None: {"status": "succeeded", "analysis_run_id": run_id}
    )

    out = service.run_task(task_id, "manual", "job-task-1", 1)
    assert out["status"] == "succeeded"
    assert captured["skills"] == ["cohort-retention"]
    assert captured["_model_choice"] == {
        "provider_id": "ds",
        "model_id": "deepseek-reasoner",
        "reasoning_effort": "high",
    }
    assert captured["reasoning_effort"] == "high"


def test_run_task_pauses_cleanly_when_saved_model_provider_is_disabled(task_api, monkeypatch):
    client, session, user, _, engine = task_api
    create_res = client.post(
        "/api/analysis/tasks",
        json={
            "title": "模型停用自动暂停",
            "question": "华东区GMV走势",
            "model": {"provider_id": "ds", "model_id": "deepseek-chat"},
            "reasoning_effort": "low",
        },
    )
    assert create_res.status_code == 201, create_res.text
    task_id = create_res.json()["id"]

    provider = session.get(ModelProvider, "ds")
    provider.enabled = False
    job = Job(
        id="job-task-disabled-model",
        kind="analysis",
        payload={"task_id": task_id},
        status="running",
        attempts=1,
        created_by=user.id,
    )
    session.add(job)
    session.commit()

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(service, "session_scope", lambda: _ctx(factory))
    monkeypatch.setattr(service, "warm_up", lambda: None)

    out = service.run_task(task_id, "schedule", "job-task-disabled-model", 1)
    assert out["status"] == "paused"
    assert "所选模型不可用于分析" in out["reason"]

    session.expire_all()
    task = session.get(AnalysisTask, task_id)
    assert task is not None
    assert task.status == "paused"
    assert task.paused_reason == out["reason"]
    updated_job = session.get(Job, "job-task-disabled-model")
    assert updated_job is not None
    assert updated_job.status == "skipped"


def test_task_rejects_whitespace_only_question(task_api):
    client, session, user, _, _ = task_api

    ws_create = client.post("/api/analysis/tasks", json={"title": "ws", "question": "   "})
    assert ws_create.status_code == 422

    short_create = client.post("/api/analysis/tasks", json={"title": "short", "question": " a "})
    assert short_create.status_code == 422

    created = client.post("/api/analysis/tasks", json={"question": "分析近30天营收趋势"})
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]

    ws_patch = client.patch(f"/api/analysis/tasks/{task_id}", json={"question": "   "})
    assert ws_patch.status_code == 422

    empty_patch = client.patch(f"/api/analysis/tasks/{task_id}", json={"question": ""})
    assert empty_patch.status_code == 422

    null_patch = client.patch(f"/api/analysis/tasks/{task_id}", json={"question": None})
    assert null_patch.status_code == 200
    assert null_patch.json()["question"] == "分析近30天营收趋势"

    with pytest.raises(Invalid):
        service.create_task(session, user, "ws", "   ", None, None)

    task = session.get(AnalysisTask, task_id)
    with pytest.raises(Invalid):
        service.update_task(session, user, task, {"question": "   "})
    with pytest.raises(Invalid):
        service.update_task(session, user, task, {"question": ""})


class _ctx:
    def __init__(self, factory):
        self.factory = factory
        self.session = None

    def __enter__(self):
        self.session = self.factory()
        return self.session

    def __exit__(self, exc_type, _exc, _tb):
        if exc_type:
            self.session.rollback()
        else:
            self.session.commit()
        self.session.close()
