"""M3B 迁移 0030、Token/费用账本、定价、配额与限流测试。"""

from __future__ import annotations

import importlib.util
import io
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import JSON, Column, DateTime, MetaData, String, Table, create_engine, inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from avelis import cache, usage
from avelis.analysis import service as analysis_service
from avelis.api import analysis as analysis_api
from avelis.api import ask as ask_api
from avelis.api import model_settings as model_settings_api
from avelis.api.deps import current_user, get_session
from avelis.ask import agent as ask_agent
from avelis.db import Base, utcnow
from avelis.governance.errors import GovernanceError
from avelis.models import (
    AnalysisRun,
    Answer,
    Domain,
    DomainMember,
    LlmPricing,
    LlmUsageLedger,
    User,
    UserRole,
)


@pytest.fixture(autouse=True)
def _clean_rate_limits():
    cache.clear_local_fallbacks()
    yield
    cache.clear_local_fallbacks()


def _load_0030():
    path = Path(__file__).resolve().parents[2] / "src/avelis/migrations/versions/0030_m3b_multi_model_ledger.py"
    spec = importlib.util.spec_from_file_location("migration_0030", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_0030_postgresql_ddl_sqlite_roundtrip_and_model_parity(monkeypatch):
    migration = _load_0030()
    assert migration.revision == "0030_m3b_multi_model_ledger"
    assert migration.down_revision == "0029_vqr_domain_exp_feedback"

    ddl = io.StringIO()
    ctx = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": ddl})
    with Operations.context(ctx):
        migration.upgrade()
        migration.downgrade()
    sql = ddl.getvalue()
    assert "ALTER TABLE model_assignment ADD COLUMN backups" in sql
    assert "CREATE TABLE llm_usage_ledger" in sql
    assert "CREATE TABLE llm_pricing" in sql
    assert "CREATE TABLE llm_quota" in sql
    assert "DROP TABLE llm_quota" in sql
    assert "DROP TABLE llm_pricing" in sql
    assert "DROP TABLE llm_usage_ledger" in sql

    monkeypatch.setattr(migration, "JsonType", JSON())
    engine = create_engine("sqlite://")
    meta = MetaData()
    Table("app_user", meta, Column("id", String(32), primary_key=True))
    Table("model_provider", meta, Column("id", String(32), primary_key=True))
    Table(
        "model_assignment",
        meta,
        Column("purpose", String(16), primary_key=True),
        Column("provider_id", String(32), nullable=False),
        Column("model_id", String(128), nullable=False),
        Column("reasoning_effort", String(8)),
        Column("updated_by", String(32), nullable=False),
        Column("updated_at", DateTime, nullable=False),
    )
    meta.create_all(engine)
    try:
        with engine.begin() as conn:
            mig_ctx = MigrationContext.configure(conn)
            with Operations.context(mig_ctx):
                migration.upgrade()

            insp = inspect(conn)
            tables = set(insp.get_table_names())
            assert {"llm_usage_ledger", "llm_pricing", "llm_quota"} <= tables
            ma_cols = {c["name"] for c in insp.get_columns("model_assignment")}
            assert "backups" in ma_cols
            ledger_cols = {c["name"] for c in insp.get_columns("llm_usage_ledger")}
            assert set(LlmUsageLedger.__table__.c.keys()) <= ledger_cols
            assert insp.get_foreign_keys("llm_usage_ledger") == [
                fk for fk in insp.get_foreign_keys("llm_usage_ledger") if fk["referred_table"] == "app_user"
            ]

            with Operations.context(mig_ctx):
                migration.downgrade()

            insp_after = inspect(conn)
            assert "backups" not in {c["name"] for c in insp_after.get_columns("model_assignment")}
            assert not ({"llm_usage_ledger", "llm_pricing", "llm_quota"} & set(insp_after.get_table_names()))
    finally:
        engine.dispose()


def _sqlite_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    meta = MetaData()
    for table in Base.metadata.sorted_tables:
        copied = table.to_metadata(meta)
        for col in copied.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()
    meta.create_all(engine)
    return engine


def test_pricing_computation_and_delete_conversation_with_ledger(monkeypatch):
    engine = _sqlite_session()
    try:
        with Session(engine) as s:
            admin = User(id="u-admin", username="admin", display_name="Admin", password_hash="x", is_active=True)
            s.add(admin)
            s.add(UserRole(user_id=admin.id, role="admin"))
            s.add(
                LlmPricing(
                    provider_code="custom",
                    model_id="my-model",
                    input_per_1m=Decimal("10.000000"),
                    output_per_1m=Decimal("30.000000"),
                    cache_hit_per_1m=Decimal("2.000000"),
                    currency="USD",
                )
            )
            run = AnalysisRun(
                id="run-del",
                user_id=admin.id,
                question="分析指标",
                trigger="manual",
                status="succeeded",
                window={},
                plan={},
                report={},
                verification={},
                budget={},
            )
            s.add(run)
            s.flush()

            row = usage.record_usage(
                s,
                user_id=admin.id,
                purpose="analysis",
                phase="reason",
                provider_code="custom",
                model_id="my-model",
                run_id=run.id,
                usage={"input_tokens": 1000, "output_tokens": 500, "cache_hit_tokens": 400, "reasoning_tokens": 100},
            )
            assert row is not None
            assert row.total_tokens == 1500
            # non-cached 600 * 10 + cached 400 * 2 + output 500 * 30 = 6000 + 800 + 15000 = 21800 / 1_000_000 = 0.0218
            assert row.estimated_cost == Decimal("0.021800")

            # clamp cache_hit > input safely
            clamped = usage.compute_cost(100, 50, 200, Decimal("10"), Decimal("20"), Decimal("2"))
            assert clamped == Decimal("0.001400")

            # builtin prefix match works for dated model IDs
            in_p, out_p, _, _, src = usage.lookup_pricing(s, None, "claude-sonnet-4-5-20250929")
            assert src == "builtin" and in_p == Decimal("3.00") and out_p == Decimal("15.00")

            # delete_conversation must succeed even though llm_usage_ledger references run.id
            monkeypatch.setattr(ask_agent, "checkpointer", lambda: SimpleNamespace(delete_thread=lambda *_: None))
            analysis_service.delete_conversation(s, admin, run.id)
            s.commit()
            assert s.get(AnalysisRun, run.id) is None
            assert s.get(LlmUsageLedger, row.id) is not None
    finally:
        engine.dispose()


def test_quotas_rate_limits_and_usage_api(monkeypatch):
    engine = _sqlite_session()
    try:
        with Session(engine) as s:
            admin = User(id="u-admin", username="admin", display_name="Admin", password_hash="x", is_active=True)
            analyst = User(
                id="u-analyst", username="analyst", display_name="Analyst", password_hash="x", is_active=True
            )
            dom = Domain(id="d1", code="fin", name="财务", owner_id=admin.id)
            s.add_all(
                [
                    admin,
                    analyst,
                    dom,
                    UserRole(user_id=admin.id, role="admin"),
                    UserRole(user_id=analyst.id, role="analyst"),
                    DomainMember(domain_id=dom.id, user_id=analyst.id, role="viewer"),
                ]
            )
            s.commit()

        app = FastAPI()
        app.include_router(model_settings_api.router)
        app.include_router(ask_api.router)
        app.include_router(analysis_api.router)

        @app.exception_handler(GovernanceError)
        async def _gov(_: Request, exc: GovernanceError):
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.message, **exc.detail})

        active_user = {"id": "u-admin"}

        def _session_override():
            with Session(engine) as s:
                yield s
                s.commit()

        def _user_override():
            with Session(engine) as s:
                return s.get(User, active_user["id"])

        app.dependency_overrides[get_session] = _session_override
        app.dependency_overrides[current_user] = _user_override

        monkeypatch.setattr(analysis_service, "warm_up", lambda: None)
        monkeypatch.setattr(analysis_api.jobs, "_wake", lambda *_: None)
        monkeypatch.setattr(
            ask_agent,
            "run_turn",
            lambda session, user, q, tid, **kw: Answer(
                id="ans-1",
                question=q,
                asked_by=user.id,
                asked_at=utcnow(),
                status="answered",
                model_adapter="demo",
                intent={},
                plan={},
                metric_version_ids=[],
                evidence={},
                result={},
                freshness={},
                cache_hit=False,
                steps=[],
            ),
        )

        client = TestClient(app)

        # Admin configures pricing & user quota
        r_price = client.put(
            "/api/model-pricing",
            json={
                "items": [
                    {
                        "provider_code": "ds",
                        "model_id": "deepseek-chat",
                        "input_per_1m": 1.0,
                        "output_per_1m": 2.0,
                        "cache_hit_per_1m": 0.5,
                        "currency": "USD",
                    }
                ]
            },
        )
        assert r_price.status_code == 200
        assert len(r_price.json()["configured"]) == 1

        r_quota = client.put(
            "/api/llm-quotas",
            json={
                "scope_type": "user",
                "scope_id": "u-analyst",
                "daily_token_limit": 1000,
                "rpm_limit": 2,
                "enabled": True,
            },
        )
        assert r_quota.status_code == 200
        quota_id = r_quota.json()["id"]

        # Record usage for both admin and analyst
        with Session(engine) as s:
            usage.record_usage(
                s,
                user_id="u-admin",
                purpose="analysis",
                provider_code="ds",
                model_id="deepseek-chat",
                usage={"input_tokens": 500, "output_tokens": 500},
                status="failover",
                failover_from="openai:gpt-4o",
            )
            usage.record_usage(
                s,
                user_id="u-analyst",
                domain_id="d1",
                purpose="ask",
                provider_code="ds",
                model_id="deepseek-chat",
                usage={"input_tokens": 400, "output_tokens": 200},
            )
            s.commit()

        # Analyst can only see own usage in summary & ledger
        active_user["id"] = "u-analyst"
        assert client.put("/api/llm-quotas", json={"scope_type": "global", "rpm_limit": 5}).status_code == 403
        me_summary = client.get("/api/llm-usage/summary").json()
        assert me_summary["total_tokens"] == 600 and me_summary["total_calls"] == 1
        me_status = client.get("/api/llm-usage/me").json()
        assert me_status["today_tokens"] == 600 and len(me_status["quotas"]) == 1

        # RPM limit (2 per min) enforced on /api/ask
        assert client.post("/api/ask", json={"question": "q1"}).status_code == 200
        assert client.post("/api/ask", json={"question": "q2"}).status_code == 200
        r_429 = client.post("/api/ask/stream", json={"question": "q3"})
        assert r_429.status_code == 429
        assert r_429.headers.get("retry-after")
        assert r_429.json()["detail"]["code"] == "rate_limited"

        # Exceed daily token quota -> 429 quota_exceeded on Ask & Analysis
        cache.clear_local_rate_limits()
        with Session(engine) as s:
            usage.record_usage(
                s,
                user_id="u-analyst",
                purpose="ask",
                provider_code="ds",
                model_id="deepseek-chat",
                usage={"input_tokens": 500, "output_tokens": 0},
            )
            s.commit()

        r_q_ask = client.post("/api/ask", json={"question": "q4"})
        assert r_q_ask.status_code == 429 and r_q_ask.json()["detail"]["code"] == "quota_exceeded"
        r_q_ana = client.post("/api/analysis/runs", json={"question": "分析q4"})
        assert r_q_ana.status_code == 429 and r_q_ana.json()["detail"]["code"] == "quota_exceeded"

        # Admin views all summary, ledger & deletes quota
        active_user["id"] = "u-admin"
        all_sum = client.get("/api/llm-usage/summary").json()
        assert all_sum["total_calls"] == 3 and all_sum["failover_count"] == 1
        ledger = client.get("/api/llm-usage/ledger?status=failover").json()
        assert ledger["total"] == 1 and ledger["items"][0]["failover_from"] == "openai:gpt-4o"
        assert client.delete(f"/api/llm-quotas/{quota_id}").status_code == 204
    finally:
        engine.dispose()


def test_m3b_quota_multi_domain_rpm_zero_and_mixed_currency():
    from fastapi import HTTPException

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        execution_options={"schema_translate_map": {"avelis_agent": None}},
    )
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()
    Base.metadata.create_all(engine)
    try:
        cache.clear_local_rate_limits()
        with Session(engine) as s:
            u_multi = User(id="u-m1", username="multi", display_name="Multi", password_hash="h", is_active=True)
            u_zero = User(id="u-z1", username="zero", display_name="Zero", password_hash="h", is_active=True)
            d_cap = Domain(id="d-cap", code="cap", name="受限域", owner_id="u-m1")
            d_free = Domain(id="d-free", code="free", name="未限域", owner_id="u-m1")
            s.add_all(
                [
                    u_multi,
                    u_zero,
                    d_cap,
                    d_free,
                    DomainMember(domain_id="d-cap", user_id="u-m1", role="viewer"),
                    DomainMember(domain_id="d-free", user_id="u-m1", role="viewer"),
                ]
            )
            usage.upsert_quota(
                s,
                u_multi,
                {"scope_type": "domain", "scope_id": "d-cap", "daily_token_limit": 100, "enabled": True},
            )
            usage.upsert_quota(
                s,
                u_multi,
                {"scope_type": "user", "scope_id": "u-z1", "rpm_limit": 0, "enabled": True},
            )
            usage.record_usage(
                s,
                user_id="u-m1",
                domain_id="d-cap",
                purpose="ask",
                provider_code="ds",
                model_id="deepseek-chat",
                usage={"input_tokens": 120, "output_tokens": 0},
                status="ok",
            )
            usage.record_usage(
                s,
                user_id="u-zero",
                purpose="analysis",
                provider_code="qwen",
                model_id="qwen-max",
                usage={"input_tokens": 50, "output_tokens": 50},
                status="ok",
            )
            # 构造不同币种的两条记录以验证 cost_by_currency 与 MIXED
            s.add(
                LlmUsageLedger(
                    user_id="u-m1",
                    purpose="ask",
                    provider_code="ds",
                    model_id="deepseek-chat",
                    input_tokens=10,
                    output_tokens=10,
                    total_tokens=20,
                    estimated_cost=Decimal("1.500000"),
                    currency="USD",
                    status="ok",
                )
            )
            s.add(
                LlmUsageLedger(
                    user_id="u-m1",
                    purpose="analysis",
                    provider_code="qwen",
                    model_id="qwen-max",
                    input_tokens=10,
                    output_tokens=10,
                    total_tokens=20,
                    estimated_cost=Decimal("10.000000"),
                    currency="CNY",
                    status="ok",
                )
            )
            s.commit()

            # 用户同时在受限域与未限域时，受限域超额仍应被拒绝（禁止多域绕过）
            with pytest.raises(HTTPException) as exc_quota:
                usage.check_quota(s, u_multi)
            assert exc_quota.value.status_code == 429
            assert exc_quota.value.detail["code"] == "quota_exceeded"

            # 显式配额 rpm_limit == 0 时拒绝请求，而非视为不限流
            with pytest.raises(HTTPException) as exc_rpm:
                usage.enforce_llm_preflight(None, s, u_zero, action="ask", domain_ids=[])
            assert exc_rpm.value.status_code == 429
            assert exc_rpm.value.detail["code"] == "rate_limited"

            summary = usage.usage_summary(s, user_id="u-m1", days=30)
            assert summary["currency"] == "MIXED"
            assert summary["cost_by_currency"]["CNY"] == pytest.approx(10.0)
            assert summary["cost_by_currency"]["USD"] >= 1.5
    finally:
        engine.dispose()
