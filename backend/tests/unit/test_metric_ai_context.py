"""口径级语义提示：HTTP 契约、权限、审计、检索与执行边界。"""

import json
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import JSON, MetaData, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from avelis import cache
from avelis.analysis.tools import AnalysisToolbox, Budget
from avelis.api.app import create_app
from avelis.api.deps import current_user, get_session
from avelis.ask import service
from avelis.ask.agent import Toolbox
from avelis.ask.intent import DemoIntentModel, Intent, MetricCard
from avelis.connectors.base import QueryResult
from avelis.db import Base
from avelis.engines.base import SqlDialect
from avelis.governance import metrics
from avelis.models import (
    Asset,
    AuditEvent,
    Datasource,
    Domain,
    DomainMember,
    Metric,
    MetricVersion,
    MetricVersionTransition,
    User,
    UserRole,
)

EMPTY = {"instructions": None, "synonyms": [], "examples": []}
CONTEXT = {"instructions": "仅用于范围甲；范围乙需先澄清。", "synonyms": ["合成词甲"], "examples": ["观察序列丙"]}


@pytest.fixture
def api():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for original in Base.metadata.tables.values():
        table = original.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(engine)
    app = create_app()
    with Session(engine, expire_on_commit=False) as session:
        users = {
            name: User(id=name, username=name, display_name=name, password_hash="unused")
            for name in ("owner", "maintainer", "domain_owner", "admin", "reader", "outsider")
        }
        session.add_all(list(users.values()))
        session.add_all(
            [
                Domain(id="visible", code="visible", name="域甲", owner_id="domain_owner"),
                Domain(id="hidden", code="hidden", name="域乙", owner_id="outsider"),
                UserRole(user_id="admin", role="admin"),
                UserRole(user_id="maintainer", role="steward"),
                UserRole(user_id="owner", role="analyst"),
            ]
        )
        for name, role in (
            ("owner", "viewer"),
            ("maintainer", "maintainer"),
            ("domain_owner", "owner"),
            ("reader", "viewer"),
        ):
            session.add(DomainMember(user_id=name, domain_id="visible", role=role))
        session.add_all(
            [
                Datasource(id="ds", name="离线源", kind="postgres", host="offline", port=1),
                Asset(
                    id="asset",
                    datasource_id="ds",
                    catalog_name="catalog",
                    db_name="schema_a",
                    name="records",
                    kind="table",
                    domain_id="visible",
                    current_version_id="asset-v1",
                ),
                Metric(
                    id="metric",
                    code="metric_a",
                    name="指标甲",
                    domain_id="visible",
                    owner_id="owner",
                    created_by="maintainer",
                ),
                Metric(
                    id="hidden_metric",
                    code="metric_b",
                    name="指标乙",
                    domain_id="hidden",
                    owner_id="owner",
                    created_by="maintainer",
                ),
                MetricVersion(
                    id="v1",
                    metric_id="metric",
                    version_no=1,
                    status="published",
                    description="说明甲",
                    base_asset_id="asset",
                    expression="SUM(b.value_a)",
                    time_column="b.day_a",
                    timezone="UTC",
                    created_by="maintainer",
                    dependency_fingerprint="bound-fingerprint",
                ),
                MetricVersion(
                    id="v2",
                    metric_id="metric",
                    version_no=2,
                    status="pending_review",
                    description="说明乙",
                    base_asset_id="asset",
                    expression="SUM(b.value_a)",
                    time_column="b.day_a",
                    timezone="UTC",
                    created_by="maintainer",
                ),
            ]
        )
        session.commit()
        actor = [users["owner"]]
        app.dependency_overrides[current_user] = lambda: actor[0]

        def db_session():
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

        app.dependency_overrides[get_session] = db_session
        with TestClient(app) as client:
            yield SimpleNamespace(client=client, session=session, users=users, actor=actor)
    engine.dispose()


def put(api, body, metric_id="metric"):
    return api.client.put(f"/api/metrics/{metric_id}/ai-context", json=body)


def test_replace_clean_clear_and_persist(api):
    assert api.client.get("/api/metrics/metric").json()["ai_context"] == EMPTY
    assert api.client.get("/api/metrics").json()[0]["ai_context"] == EMPTY
    assert api.session.get(Metric, "metric").ai_context == {}
    response = put(
        api,
        {
            "instructions": CONTEXT["instructions"],
            "synonyms": [" 合成词甲 ", "", "合成词甲", "\t", "合成词乙"],
            "examples": [" 观察序列丙 ", "观察序列丙", "\n"],
        },
    )
    expected = {**CONTEXT, "synonyms": ["合成词甲", "合成词乙"]}
    assert response.status_code == 200, response.text
    assert response.json()["ai_context"] == expected
    assert response.json() == api.client.get("/api/metrics/metric").json()
    api.session.expire_all()
    assert api.session.get(Metric, "metric").ai_context == expected
    assert put(api, {"synonyms": ["替代词"]}).json()["ai_context"] == {**EMPTY, "synonyms": ["替代词"]}
    assert put(api, {}).json()["ai_context"] == EMPTY


@pytest.mark.parametrize("actor", ["owner", "maintainer", "domain_owner", "admin"])
def test_edit_permissions(api, actor):
    api.actor[0] = api.users[actor]
    assert put(api, CONTEXT).status_code == 200


@pytest.mark.parametrize(
    "actor,metric_id,status",
    [
        ("reader", "metric", 403),
        ("outsider", "metric", 404),
        ("owner", "hidden_metric", 404),
        ("owner", "missing", 404),
    ],
)
def test_rejected_edits_do_not_write(api, actor, metric_id, status):
    api.actor[0] = api.users[actor]
    assert put(api, CONTEXT, metric_id).status_code == status
    assert api.session.get(Metric, "metric").ai_context == {}
    assert list(api.session.scalars(select(AuditEvent))) == []


@pytest.mark.parametrize(
    "body",
    [
        {"instructions": "字" * 2001},
        {"synonyms": ["字" * 65]},
        {"examples": ["字" * 201]},
        {"synonyms": [f"词{i}" for i in range(31)]},
        {"examples": [f"问法{i}" for i in range(21)]},
        {"synonyms": None},
        {"examples": [1]},
        {"instructions": 1},
        {"extra": "ignored?"},
        {"synonyms": "词甲"},
        {"examples": [{}]},
    ],
)
def test_invalid_context_is_422_on_create_and_update(api, body):
    assert put(api, body).status_code == 422
    api.actor[0] = api.users["maintainer"]
    response = api.client.post(
        "/api/metrics",
        json={
            "code": "new_metric",
            "name": "指标丙",
            "domain_id": "visible",
            "ai_context": body,
        },
    )
    assert response.status_code == 422
    assert list(api.session.scalars(select(AuditEvent))) == []


def test_limits_apply_after_cleaning_and_allow_exact_boundaries(api):
    body = {
        "instructions": "字" * 2000,
        "synonyms": [" " + "字" * 64 + " "] * 31 + [""] * 10,
        "examples": [" " + "字" * 200 + " "] * 21 + [" "] * 10,
    }
    response = put(api, body)
    assert response.status_code == 200, response.text
    assert response.json()["ai_context"] == {
        "instructions": "字" * 2000,
        "synonyms": ["字" * 64],
        "examples": ["字" * 200],
    }
    assert (
        put(api, {"synonyms": [f"词{i}" for i in range(30)], "examples": [f"问法{i}" for i in range(20)]}).status_code
        == 200
    )


def test_create_accepts_optional_clean_context_and_audits_it(api):
    api.actor[0] = api.users["maintainer"]
    for code, context in (("empty_metric", None), ("new_metric", {"synonyms": [" 合成词甲 ", "合成词甲", " "]})):
        response = api.client.post(
            "/api/metrics",
            json={
                "code": code,
                "name": "指标丙",
                "domain_id": "visible",
                "ai_context": context,
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["ai_context"] == (EMPTY if context is None else {**EMPTY, "synonyms": ["合成词甲"]})
    event = api.session.scalar(select(AuditEvent).where(AuditEvent.action == "metric.ai_context"))
    assert event.actor_id == "maintainer"
    assert event.detail == {"before": EMPTY, "after": {**EMPTY, "synonyms": ["合成词甲"]}}


def test_audit_records_every_edit_without_changing_versions(api):
    before = api.client.get("/api/metrics/metric").json()["versions"]
    for body in (CONTEXT, CONTEXT, {}):
        assert put(api, body).status_code == 200
    events = list(
        api.session.scalars(select(AuditEvent).where(AuditEvent.action == "metric.ai_context").order_by(AuditEvent.at))
    )
    assert [e.detail for e in events] == [
        {"before": EMPTY, "after": CONTEXT},
        {"before": CONTEXT, "after": CONTEXT},
        {"before": CONTEXT, "after": EMPTY},
    ]
    assert all(e.actor_id == "owner" and e.object_type == "metric" and e.object_id == "metric" for e in events)
    assert api.client.get("/api/metrics/metric").json()["versions"] == before
    assert list(api.session.scalars(select(MetricVersionTransition))) == []


@pytest.mark.parametrize("term", ["合成词甲", "观察序列丙"])
def test_synonyms_and_examples_reach_ask_and_analysis(api, monkeypatch, term):
    assert put(api, CONTEXT).status_code == 200
    catalog = service.visible_catalog(api.session, api.users["owner"], with_values=False)
    assert catalog.cards[0].ai_context == CONTEXT
    question = f"2026-01-01 {term}"
    intent = DemoIntentModel().parse(question, catalog.cards, today=date(2026, 1, 1))
    assert intent.metric_code == "metric_a" and not intent.clarification
    found = json.loads(Toolbox(api.session, api.users["owner"], catalog).search_metrics(term))
    assert [item["code"] for item in found] == ["metric_a"]
    assert found[0]["synonyms"] == CONTEXT["synonyms"]
    assert found[0]["instructions"] == CONTEXT["instructions"]
    box = AnalysisToolbox(api.session, api.users["owner"], SimpleNamespace(id="run"), catalog, Budget())
    # 填充超过全量候选阈值的干扰项，证明命中来自分数而非全量返回。
    distractors = [MetricCard(code=f"a{i:02}", name="干扰项", aliases=[], dimensions=[]) for i in range(40)]
    extended = service.Catalog(
        distractors + catalog.cards,
        {
            **{c.code: catalog.versions["metric_a"] for c in distractors},
            **catalog.versions,
        },
    )
    monkeypatch.setattr(service, "visible_catalog", lambda *args, **kwargs: extended)
    candidates = box.candidate_metrics(question)
    assert candidates[0]["code"] == "metric_a"
    assert candidates[0]["ai_context"] == CONTEXT
    found = json.loads(box.search_metrics(term))
    assert [item["code"] for item in found["metrics"]] == ["metric_a"]
    assert found["metrics"][0]["ai_context"] == CONTEXT


def test_candidate_synonyms_have_name_weight_and_examples_have_lower_weight(api, monkeypatch):
    term = "合成词甲"
    cards = [
        MetricCard(code="a_example", name="指标甲", aliases=[], dimensions=[], ai_context={"examples": [term]}),
        MetricCard(code="b_synonym", name="指标乙", aliases=[], dimensions=[], ai_context={"synonyms": [term]}),
        MetricCard(code="c_alias", name="指标丙", aliases=[term], dimensions=[]),
        MetricCard(code="d_name", name=term, aliases=[], dimensions=[]),
    ]
    metric, version = api.session.get(Metric, "metric"), api.session.get(MetricVersion, "v1")
    catalog = service.Catalog(cards, {c.code: (metric, version) for c in cards})
    monkeypatch.setattr(service, "visible_catalog", lambda *args, **kwargs: catalog)
    box = AnalysisToolbox(api.session, api.users["owner"], SimpleNamespace(id="run"), catalog, Budget())
    assert [c["code"] for c in box.candidate_metrics(term)] == ["b_synonym", "c_alias", "d_name", "a_example"]


def test_candidate_examples_capped_and_describe_context_preserved(api, monkeypatch):
    context = {**CONTEXT, "examples": [f"问法{i}" for i in range(5)]}
    assert put(api, context).status_code == 200
    catalog = service.visible_catalog(api.session, api.users["owner"], with_values=False)
    box = AnalysisToolbox(api.session, api.users["owner"], SimpleNamespace(id="run"), catalog, Budget())
    assert box.candidate_metrics("问法4")[0]["ai_context"] == {**context, "examples": context["examples"][:3]}
    metric, version = catalog.versions["metric_a"]
    monkeypatch.setattr(box, "_metric_sources", lambda code: (metric, version, [], {}))
    monkeypatch.setattr("avelis.api.governance.version_view", lambda *args: {})
    detail = json.loads(box.describe_metric("metric_a"))
    assert detail["ai_context"] == context
    box.precheck_metric = detail
    pack = box.context_pack()
    assert json.loads(pack.removeprefix("口径："))["ai_context"] == context


def test_context_does_not_change_fingerprint_sql_or_real_answer_cache_key(api, monkeypatch):
    session, user = api.session, api.users["owner"]
    metric, version = session.get(Metric, "metric"), session.get(MetricVersion, "v1")
    asset = session.get(Asset, "asset")
    resolved = {"aliases": {"b": asset}, "steps": [], "dependency": []}
    monkeypatch.setattr(service, "describe_version", lambda *args: {})
    monkeypatch.setattr(service, "_version_evidence", lambda *args: {})
    monkeypatch.setattr(service, "dependency_drift", lambda *args: [])
    monkeypatch.setattr(service, "validate_spec", lambda *args: resolved)
    monkeypatch.setattr(service, "upstream_closure", lambda *args: {asset.id: asset})
    monkeypatch.setattr(service, "_column_type", lambda *args: "date")
    monkeypatch.setattr(service, "_columns", lambda *args: ["day_a", "value_a"])
    monkeypatch.setattr(service, "dialect_of", lambda *args: SqlDialect("postgres", "catalog"))
    monkeypatch.setattr(service, "_live_check", lambda *args: [])
    monkeypatch.setattr(service, "freshness_boundary", lambda *args: {})
    monkeypatch.setattr(service, "_asset_evidence", lambda *args: {})
    monkeypatch.setattr(service, "_lineage_evidence", lambda *args: [])
    monkeypatch.setattr(service, "_dimension_docs", lambda *args: {})
    connector = SimpleNamespace(
        freshness=lambda *args: {},
        execute_select=lambda *args, **kwargs: QueryResult(columns=["value"], rows=[[7]], truncated=False),
    )
    monkeypatch.setattr(service, "query_connector", lambda *args: connector)
    # 使用真实 fulfil、SQL 编译/校验与缓存键构造，只替换外部来源与缓存存储。
    stored, keys, parts = {}, [], []
    real_cache_key = cache.answer_cache_key

    def cache_key(value):
        parts.append(value)
        return real_cache_key(value)

    monkeypatch.setattr(service.cache, "answer_cache_key", cache_key)
    monkeypatch.setattr(service, "_cache_get", lambda key: keys.append(key) or stored.get(key))
    monkeypatch.setattr(service, "_cache_put", lambda key, value, *args: stored.__setitem__(key, value))
    catalog = service.Catalog([], {metric.code: (metric, version)})
    intent = Intent(metric_code=metric.code, start_date=date(2026, 1, 1), end_date=date(2026, 1, 2))
    fingerprint = metrics.dependency_fingerprint(version, version.dependency)
    first = service.start_answer(session, user, "问题甲", "demo", None)
    service.fulfil(session, user, intent, first, catalog)
    assert first.status == "answered", first.message
    assert put(api, CONTEXT).status_code == 200
    second = service.start_answer(session, user, "问题甲", "demo", None)
    service.fulfil(session, user, intent, second, catalog)
    assert second.status == "answered" and second.cache_hit, second.message
    assert first.sql == second.sql
    assert len(keys) == 2 and keys[0] == keys[1]
    assert parts[0] == parts[1] and "ai_context" not in parts[1]
    assert metrics.dependency_fingerprint(version, version.dependency) == fingerprint
    assert version.dependency_fingerprint == "bound-fingerprint" and version.status == "published"
