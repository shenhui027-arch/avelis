"""M3A 迁移 0029、业务域共享经验与验证查询库（VQR）治理及集成测试。"""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import JSON, Boolean, Column, DateTime, Integer, MetaData, String, Table, Text, create_engine, inspect

from avelis import config
from avelis.api import analysis as analysis_api
from avelis.api import governance as governance_api
from avelis.api.deps import current_user, get_session
from avelis.ask import agent as ask_agent
from avelis.governance import access, experiences, vqr
from avelis.governance.errors import Forbidden, GovernanceError, Invalid
from avelis.models import (
    AnalysisRun,
    Answer,
    Asset,
    AssetVersion,
    Domain,
    Experience,
    Metric,
    MetricVersion,
    User,
    VerifiedQuery,
)
from tests.unit.test_experiences import MemorySession


def _load_0029():
    path = Path(__file__).resolve().parents[2] / "src/avelis/migrations/versions/0029_vqr_domain_experience_feedback.py"
    spec = importlib.util.spec_from_file_location("migration_0029", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_0029_postgresql_ddl_sqlite_roundtrip_and_model_parity(monkeypatch):
    migration = _load_0029()
    assert migration.revision == "0029_vqr_domain_exp_feedback"
    assert migration.down_revision == "0028_analysis_task_options"

    ddl = io.StringIO()
    ctx = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": ddl})
    with Operations.context(ctx):
        migration.upgrade()
        migration.downgrade()
    sql = ddl.getvalue()
    assert "CREATE TABLE verified_query" in sql
    assert "ix_verified_query_domain_status" in sql
    assert "ALTER TABLE experience ADD COLUMN review_status VARCHAR(16) DEFAULT 'none' NOT NULL" in sql
    assert "ALTER TABLE answer ADD COLUMN feedback_rating VARCHAR(8)" in sql
    assert "DROP TABLE verified_query" in sql

    monkeypatch.setattr(migration, "JsonType", JSON())
    engine = create_engine("sqlite://")
    meta = MetaData()
    Table("app_user", meta, Column("id", String(32), primary_key=True))
    Table("domain", meta, Column("id", String(32), primary_key=True))
    Table("datasource", meta, Column("id", String(32), primary_key=True))
    Table("analysis_run", meta, Column("id", String(32), primary_key=True))
    Table(
        "experience",
        meta,
        Column("id", String(32), primary_key=True),
        Column("owner_id", String(32), nullable=False),
        Column("scope", String(16), nullable=False),
        Column("current_version", Integer, nullable=False),
        Column("status", String(16), nullable=False),
        Column("created_at", DateTime, nullable=False),
    )
    Table(
        "answer",
        meta,
        Column("id", String(32), primary_key=True),
        Column("question", Text, nullable=False),
        Column("thread_id", String(32)),
        Column("asked_by", String(32), nullable=False),
        Column("asked_at", DateTime, nullable=False),
        Column("status", String(16), nullable=False),
        Column("model_adapter", String(64), nullable=False),
        Column("intent", JSON, nullable=False),
        Column("plan", JSON, nullable=False),
        Column("sql", Text),
        Column("metric_version_ids", JSON, nullable=False),
        Column("evidence", JSON, nullable=False),
        Column("result", JSON, nullable=False),
        Column("message", Text),
        Column("error_code", String(32)),
        Column("freshness", JSON, nullable=False),
        Column("executed_at", DateTime),
        Column("duration_ms", Integer),
        Column("cache_hit", Boolean, nullable=False),
        Column("recheck_status", String(32)),
        Column("steps", JSON, nullable=False),
        Column("explanation", Text),
        Column("explanation_source", String(32)),
    )
    meta.create_all(engine)
    try:
        with engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
            insp = inspect(conn)
            assert "verified_query" in insp.get_table_names()
            for model in (VerifiedQuery, Experience, Answer):
                table = model.__table__
                cols = {c["name"]: c for c in insp.get_columns(table.name)}
                assert set(cols) == set(table.c.keys())
                for col in table.c:
                    assert cols[col.name]["nullable"] == col.nullable

            with Operations.context(MigrationContext.configure(conn)):
                migration.downgrade()
            assert "verified_query" not in inspect(conn).get_table_names()
            assert "feedback_rating" not in {c["name"] for c in inspect(conn).get_columns("answer")}
            assert "review_status" not in {c["name"] for c in inspect(conn).get_columns("experience")}
    finally:
        engine.dispose()


class GovMemorySession(MemorySession):
    def __init__(self, objects):
        super().__init__(objects)
        self.domain_roles: dict[tuple[str, str], str] = {}
        self.identity_map: dict[object, object] = {}

    def scalars(self, statement):
        from sqlalchemy.sql.elements import BinaryExpression

        entity = statement.column_descriptions[0]["entity"]
        if entity is VerifiedQuery:
            rows = [row for (cls, _), row in self.store.items() if cls is VerifiedQuery]
            return [
                row
                for row in rows
                if all(self._matches(row, c) for c in statement._where_criteria if isinstance(c, BinaryExpression))
            ]
        return super().scalars(statement)

    def scalar(self, statement):
        desc = statement.column_descriptions[0]
        entity = desc["entity"]
        params = statement.compile().params
        if entity is Domain:
            return self.store.get((Domain, params.get("id_1")))
        if desc.get("name") == "role":
            return self.domain_roles.get((params.get("domain_id_1"), params.get("user_id_1")))
        return super().scalar(statement)


@pytest.fixture
def gov_state():
    u_creator = User(id="u1", username="creator", is_active=True)
    u_reviewer = User(id="u2", username="reviewer", is_active=True)
    u_viewer = User(id="u3", username="viewer", is_active=True)
    u_outsider = User(id="u4", username="outsider", is_active=True)
    dom_a = Domain(id="domain-a", code="retail", name="零售域", owner_id="u2")
    dom_b = Domain(id="domain-b", code="fin", name="财务域", owner_id="u4")
    asset = Asset(
        id="a1",
        status="active",
        datasource_id="ds1",
        domain_id="domain-a",
        catalog_name="cat",
        db_name="dw",
        name="orders",
        current_version_id="av1",
    )
    av1 = AssetVersion(id="av1", asset_id="a1", version_no=1, key_columns=[], unique_keys=[])
    m1 = Metric(id="m1", code="metric_a", name="成交额", domain_id="domain-a", owner_id="u1", aliases=[], ai_context={})
    mv1 = MetricVersion(id="mv1", metric_id="m1", version_no=1, status="published", description="成交额")
    run1 = AnalysisRun(id="r1", user_id="u1", question="q")
    s = GovMemorySession([u_creator, u_reviewer, u_viewer, u_outsider, dom_a, dom_b, asset, av1, m1, mv1, run1])
    s.roles = {
        "u1": {"steward", "analyst"},
        "u2": {"reviewer", "steward", "analyst"},
        "u3": {"analyst"},
        "u4": {"reviewer"},
    }
    s.domains = {"u1": {"domain-a"}, "u2": {"domain-a"}, "u3": {"domain-a"}, "u4": {"domain-b"}}
    s.domain_roles = {("domain-a", "u1"): "viewer", ("domain-a", "u2"): "maintainer", ("domain-a", "u3"): "viewer"}
    return s, u_creator, u_reviewer, u_viewer, u_outsider


def test_domain_experience_sharing_and_strict_anti_self_approval(gov_state, monkeypatch):
    s, creator, reviewer, viewer, outsider = gov_state
    # 即使 allow_self_review=True，域经验与 VQR 依然严格禁止自审
    monkeypatch.setattr(
        access,
        "get_settings",
        lambda: config.build_settings({"AVELIS_SECRET": "x" * 20, "AVELIS_ALLOW_SELF_REVIEW": "true"}),
    )

    saved = experiences.save_experience(
        s,
        creator,
        kind="rule",
        content="metric_a 按门店区域对齐",
        refs=[{"object_type": "metric", "object_id": "m1"}],
        source={"run_id": "r1", "checkpoint": "assumptions", "user_text": "对齐"},
    )
    submitted = experiences.submit_experience(s, creator, saved["id"], domain_id="domain-a")
    assert submitted["scope"] == "domain" and submitted["review_status"] == "pending"

    # 提交者不能自审（即便拥有 maintainer 权限）
    s.domain_roles[("domain-a", creator.id)] = "maintainer"
    with pytest.raises(Forbidden, match="禁止自审"):
        experiences.approve_experience(s, creator, saved["id"])
    with pytest.raises(Forbidden, match="禁止自审"):
        experiences.reject_experience(s, creator, saved["id"], note="自驳回")

    # 域外成员不能审批，域内 viewer 未批准前 recall 不到
    with pytest.raises(Forbidden):
        experiences.approve_experience(s, outsider, saved["id"])
    assert experiences.recall(s, viewer, metric_codes=["metric_a"], question="门店区域") == []

    # reviewer/maintainer 批准后，域内 viewer 可召回，域外 outsider 不可见
    approved = experiences.approve_experience(s, reviewer, saved["id"], note="口径合理")
    assert approved["review_status"] == "approved"
    recalled = experiences.recall(s, viewer, metric_codes=["metric_a"], question="门店区域")
    assert [r["id"] for r in recalled] == [saved["id"]]
    assert experiences.recall(s, outsider, metric_codes=["metric_a"], question="门店区域") == []
    assert access.can_see_object(s, viewer, "experience", saved["id"])
    assert not access.can_see_object(s, outsider, "experience", saved["id"])

    # 引用资产迁移到不可见域后，recall 自动过滤该域共享经验
    s.get(Metric, "m1").domain_id = "domain-b"
    assert experiences.recall(s, viewer, metric_codes=["metric_a"], question="门店区域") == []


def test_vqr_lifecycle_sql_validation_anti_self_approval_and_api(gov_state):
    s, creator, reviewer, viewer, outsider = gov_state
    with pytest.raises(Invalid):
        vqr.validate_vqr_sql("DELETE FROM orders")
    with pytest.raises(Invalid):
        vqr.validate_vqr_sql("SELECT 1; SELECT 2")
    for bad_sql, dialect in (
        ("SELECT 'unclosed", "postgres"),
        ("SELECT /* unclosed", "postgres"),
        ("SELECT * INTO new_table FROM orders", "postgres"),
        ("SELECT * INTO new_table FROM orders", "tsql"),
        ("SELECT * FROM orders FOR UPDATE", "postgres"),
        ("SELECT * FROM orders FOR SHARE", "postgres"),
    ):
        with pytest.raises(Invalid):
            vqr.validate_vqr_sql(bad_sql, dialect=dialect)
    assert (
        vqr.validate_vqr_sql("WITH cte AS (SELECT 1 AS x) SELECT x FROM cte")
        == "WITH cte AS (SELECT 1 AS x) SELECT x FROM cte"
    )

    current = {"user": creator}
    app = FastAPI()

    @app.exception_handler(GovernanceError)
    async def governance_error(_request, exc: GovernanceError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message, **exc.detail})

    app.include_router(governance_api.router)
    app.include_router(analysis_api.router)
    app.dependency_overrides[current_user] = lambda: current["user"]
    app.dependency_overrides[get_session] = lambda: s

    with TestClient(app) as client:
        bad = client.post(
            "/api/verified-queries",
            json={"domain_id": "domain-a", "title": "bad", "question": "q", "sql_text": "DROP TABLE orders"},
        )
        assert bad.status_code == 422
        unclosed = client.post(
            "/api/verified-queries",
            json={"domain_id": "domain-a", "title": "bad", "question": "q", "sql_text": "SELECT 'unclosed"},
        )
        assert unclosed.status_code == 422
        select_into = client.post(
            "/api/verified-queries",
            json={
                "domain_id": "domain-a",
                "title": "bad",
                "question": "q",
                "sql_text": "SELECT * INTO new_table FROM orders",
            },
        )
        assert select_into.status_code == 422

        created = client.post(
            "/api/verified-queries",
            json={
                "domain_id": "domain-a",
                "title": "各区域成交额",
                "question": "各区域成交额是多少",
                "sql_text": "SELECT region, SUM(amount) FROM orders GROUP BY region",
                "metric_codes": ["metric_a"],
                "tags": ["区域"],
            },
        )
        assert created.status_code == 201, created.text
        vq_id = created.json()["id"]
        assert created.json()["status"] == "draft"

        assert client.post(f"/api/verified-queries/{vq_id}/submit").json()["status"] == "pending_review"

        # 严格禁止创建人/提交人自审
        s.roles[creator.id].add("reviewer")
        assert client.post(f"/api/verified-queries/{vq_id}/approve", json={"note": "ok"}).status_code == 403
        assert client.post(f"/api/verified-queries/{vq_id}/reject", json={"note": "bad"}).status_code == 403

        # 域外 reviewer 不可见（404）
        current["user"] = outsider
        assert client.post(f"/api/verified-queries/{vq_id}/approve", json={"note": "ok"}).status_code == 404

        # 域内其他 reviewer 驳回 -> 创建人编辑 -> 重提 -> 批准 -> 归档
        current["user"] = reviewer
        rej = client.post(f"/api/verified-queries/{vq_id}/reject", json={"note": "补全时间条件"})
        assert rej.status_code == 200 and rej.json()["status"] == "rejected"

        current["user"] = creator
        upd = client.put(
            f"/api/verified-queries/{vq_id}",
            json={"sql_text": "SELECT region, SUM(amount) FROM orders WHERE dt >= '2026-01-01' GROUP BY region"},
        )
        assert upd.status_code == 200 and upd.json()["status"] == "draft"
        assert client.post(f"/api/verified-queries/{vq_id}/submit").status_code == 200

        current["user"] = reviewer
        appr = client.post(f"/api/verified-queries/{vq_id}/approve", json={"note": "通过"})
        assert appr.status_code == 200 and appr.json()["status"] == "approved"

        # 域内 viewer 可见已批准 VQR，且 recall_vqr 仅返回只读参考数据
        current["user"] = viewer
        listed = client.get("/api/verified-queries", params={"q": "区域成交额"})
        assert listed.status_code == 200 and [x["id"] for x in listed.json()] == [vq_id]
        recalled = vqr.recall_vqr(s, viewer, question="各区域成交额", metric_codes=["metric_a"])
        assert len(recalled) == 1 and recalled[0]["id"] == vq_id and "sql_text" in recalled[0]

        current["user"] = reviewer
        arch = client.post(f"/api/verified-queries/{vq_id}/archive")
        assert arch.status_code == 200 and arch.json()["status"] == "archived"
        assert vqr.recall_vqr(s, viewer, question="各区域成交额") == []


def test_ask_and_analysis_inject_vqr_as_readonly_reference_only(gov_state, monkeypatch):
    from langchain_core.messages import AIMessage

    from avelis.analysis.tools import AnalysisToolbox, Budget
    from avelis.ask import service as ask_service
    from avelis.ask.intent import MetricCard

    s, creator, reviewer, viewer, _ = gov_state
    created = vqr.create_vqr(
        s,
        creator,
        domain_id="domain-a",
        title="各区域成交额",
        question="各区域成交额是多少",
        sql_text="SELECT region, SUM(amount) FROM orders GROUP BY region",
        metric_codes=["metric_a"],
    )
    vqr.submit_vqr(s, creator, created["id"])
    vqr.approve_vqr(s, reviewer, created["id"], note="通过")

    # Ask: understand() 注入 vqr_refs 到 answer.evidence，并作为只读参考传给模型，仍要求 submit_intent
    card = MetricCard(code="metric_a", name="成交额", domain="零售域", aliases=[], dimensions=[], status="published")
    catalog = ask_service.Catalog([card], {"metric_a": (s.get(Metric, "m1"), s.get(MetricVersion, "mv1"))})
    monkeypatch.setattr(ask_service, "visible_catalog", lambda *a, **kw: catalog)
    seen_prompts = []

    def fake_invoke(msgs):
        seen_prompts.append(msgs)
        return AIMessage(
            "",
            tool_calls=[
                {
                    "id": "t1",
                    "name": "submit_intent",
                    "args": {"metric_codes": ["metric_a"], "start_date": "2026-01-01", "end_date": "2026-01-31"},
                }
            ],
        )

    fake_model = SimpleNamespace(bind_tools=lambda _: SimpleNamespace(invoke=fake_invoke))
    monkeypatch.setattr(
        ask_agent.modelcfg, "resolve", lambda *a, **kw: SimpleNamespace(model=fake_model, adapter="test")
    )
    monkeypatch.setattr(ask_agent.modelcfg, "policy", lambda *a: {"send_results": False})
    answer = SimpleNamespace(evidence={})
    out = ask_agent.understand(
        {"turns": ["各区域成交额是多少"], "messages": []},
        {"configurable": {"session": s, "user": viewer, "answer": answer, "request_locale": "zh-CN"}},
    )
    assert out["intent"]["metric_codes"] == ["metric_a"]
    assert answer.evidence["vqr_refs"][0]["id"] == created["id"]
    assert any("已验证参考查询" in str(m.content) for m in seen_prompts[0])

    # Analysis: recall_vqr 注入 context_pack 只读参考块
    a_box = AnalysisToolbox(s, viewer, SimpleNamespace(id="r1"), catalog, Budget())
    a_box.recall_vqr("各区域成交额", None)
    pack = a_box.context_pack()
    assert "已验证参考查询" in pack and created["id"] in pack


def test_vqr_and_experience_remediation_guards(gov_state):
    from datetime import timedelta

    s, creator, reviewer, viewer, outsider = gov_state
    # 1. SQL Server / T-SQL 校验放行，伪造来源 ID 拒绝
    assert "SELECT" in vqr.validate_vqr_sql("SELECT TOP 5 region FROM orders", dialect="sqlserver")
    with pytest.raises(Invalid):
        vqr.create_vqr(
            s,
            creator,
            domain_id="domain-a",
            title="missing ans",
            question="q",
            sql_text="SELECT region FROM orders",
            source_answer_id="no-such-answer",
        )
    s.store[(AnalysisRun, "run-other")] = AnalysisRun(id="run-other", user_id="u2", question="q2")
    with pytest.raises(Forbidden):
        vqr.create_vqr(
            s,
            creator,
            domain_id="domain-a",
            title="foreign run",
            question="q",
            sql_text="SELECT region FROM orders",
            source_run_id="run-other",
        )

    # 2. 编辑过 VQR 的 reviewer 同样禁止自审；viewer 无权归档
    created = vqr.create_vqr(
        s,
        creator,
        domain_id="domain-a",
        title="区域复核",
        question="区域销售",
        sql_text="SELECT region, SUM(amount) FROM orders GROUP BY region",
    )
    vqr.update_vqr(s, reviewer, created["id"], notes="reviewer edited SQL")
    vqr.submit_vqr(s, creator, created["id"])
    with pytest.raises(Forbidden):
        vqr.approve_vqr(s, reviewer, created["id"], note="self approve")
    with pytest.raises(Forbidden):
        vqr.archive_vqr(s, viewer, created["id"])

    # 3. 经验提交跨业务域引用拒绝；本人待审核域经验在 recall 中仍对本人可见
    asset_b = Asset(
        id="a-b",
        status="active",
        datasource_id="ds1",
        domain_id="domain-b",
        catalog_name="cat",
        db_name="dw",
        name="fin_orders",
        current_version_id="av-b",
    )
    av_b = AssetVersion(id="av-b", asset_id="a-b", version_no=1, key_columns=[], unique_keys=[])
    s.store[(Asset, "a-b")] = asset_b
    s.store[(AssetVersion, "av-b")] = av_b
    s.domains["u1"].add("domain-b")
    exp_cross = experiences.save_experience(
        s,
        creator,
        kind="rule",
        content="引用财务域资产",
        refs=[{"object_type": "asset", "object_id": "a-b"}],
        source={"run_id": "r1", "checkpoint": "assumptions", "user_text": "跨域引用"},
    )
    with pytest.raises(Forbidden):
        experiences.submit_experience(s, creator, exp_cross["id"], domain_id="domain-a")

    exp_a = experiences.save_experience(
        s,
        creator,
        kind="rule",
        content="metric_a 待审期间本人仍可召回",
        refs=[{"object_type": "metric", "object_id": "m1"}],
        source={"run_id": "r1", "checkpoint": "assumptions", "user_text": "待审召回"},
    )
    experiences.submit_experience(s, creator, exp_a["id"], domain_id="domain-a")
    recalled_owner = experiences.recall(s, creator, metric_codes=["metric_a"])
    assert any(item["id"] == exp_a["id"] for item in recalled_owner)
    assert all(item["id"] != exp_a["id"] for item in experiences.recall(s, viewer, metric_codes=["metric_a"]))
    assert isinstance(timedelta(days=1), timedelta)
