"""用内存 SQLite 验证血缘体检的快照、接口和模型复核。"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import JSON, MetaData, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from avelis import modelcfg
from avelis.api import catalog
from avelis.api.deps import current_user, get_session
from avelis.db import Base
from avelis.governance import lineage_health
from avelis.governance.errors import GovernanceError
from avelis.i18n import translate
from avelis.models import (
    AppSetting,
    Asset,
    AssetColumn,
    AssetVersion,
    AuditEvent,
    CandidateEdge,
    CollectionRun,
    Datasource,
    Definition,
    Domain,
    DomainMember,
    ImportJob,
    LineageEdge,
    ParseResult,
    User,
    UserRole,
)

HEALTH_URL = "/api/lineage/health"
REVIEW_URL = f"{HEALTH_URL}/review"
PARSE_REASON = "表达式 RAW_PARSE_SQL 未展开"
EXPRESSION = "RAW_EDGE_EXPRESSION"
INVALID_REVIEW = "模型没有返回可用的复核结论，请稍后再试"


def _seed_snapshot(session, user):
    def asset(name, *, domain_id="visible", kind="table", **kwargs):
        return Asset(
            id=name,
            datasource_id="ds",
            kind=kind,
            catalog_name="c",
            db_name="dw",
            name=name,
            domain_id=domain_id,
            **kwargs,
        )

    def edge(edge_id, source, target, source_column, target_column, **kwargs):
        return LineageEdge(
            id=edge_id,
            source_asset_id=source,
            source_ref=source,
            source_column=source_column,
            target_asset_id=target,
            target_column=target_column,
            relation="value_direct",
            origin="import",
            **kwargs,
        )

    session.add_all(
        [
            user,
            User(id="owner", username="owner", display_name="其他域负责人", password_hash="unused"),
            Domain(id="visible", code="visible", name="可见域", owner_id=user.id),
            Domain(id="hidden", code="hidden", name="其他域", owner_id="owner"),
            DomainMember(domain_id="visible", user_id=user.id, role="viewer"),
            Datasource(id="ds", name="测试数据源", kind="fake", host="localhost", port=1),
            CollectionRun(id="r", datasource_id="ds", trigger="manual", status="done"),
            AppSetting(key=modelcfg.POLICY_KEY, value={"send_code": True}),
            asset("orders"),
            asset("daily", kind="view", current_version_id="v2"),
            asset("archive"),
            asset("payroll", domain_id="hidden"),
            asset("inactive", status="deleted"),
            AssetColumn(asset_id="orders", name="amount"),
            AssetColumn(asset_id="orders", name="legacy_amount", status="deleted"),
            AssetColumn(asset_id="daily", name="total"),
            edge("e-orders", "orders", "daily", "legacy_amount", "total", expression=EXPRESSION),
            edge("e-archive", "daily", "archive", "total", "obsolete"),
            edge("e-hidden-source", "payroll", "daily", "salary", "unknown"),
            edge("e-hidden-target", "daily", "payroll", "unknown", "salary"),
            edge("e-inactive", "orders", "daily", "unknown", "total", status="superseded"),
        ]
    )
    for n in (1, 2):
        session.add_all(
            [
                AssetVersion(
                    id=f"v{n}",
                    asset_id="daily",
                    version_no=n,
                    observed_run_id="r",
                    last_seen_run_id="r",
                    kind="view",
                    catalog_name="c",
                    db_name="dw",
                    name="daily",
                    content_hash=f"hash-{n}",
                ),
                Definition(
                    id=f"d{n}",
                    asset_version_id=f"v{n}",
                    dialect="postgres",
                    raw_sql="SELECT 1",
                    canonical_hash=f"sql-hash-{n}",
                ),
            ]
        )
    for definition, parser, day, status, reason in (
        ("d1", "parser-3", 3, "failed", "旧定义的解析结果"),
        ("d2", "parser-1", 1, "ok", None),
        ("d2", "parser-2", 2, "partial", PARSE_REASON),
    ):
        session.add(
            ParseResult(
                definition_id=definition,
                parser_version=parser,
                created_at=datetime(2026, 1, day, tzinfo=UTC),
                status=status,
                reason=reason,
            )
        )
    for job_id, revision, status in (("old", 1, "superseded"), ("current", 2, "needs_review")):
        session.add(
            ImportJob(
                id=job_id,
                datasource_id="ds",
                source_key="daily.sql",
                revision=revision,
                source_type="sql",
                filename="daily.sql",
                language="sql",
                dialect="postgres",
                content_hash=f"import-hash-{revision}",
                status=status,
                created_by=user.id,
            )
        )
    for candidate_id, job_id, target, status in (
        ("candidate-current", "current", "daily", "pending"),
        ("candidate-old", "old", "daily", "pending"),
        ("candidate-accepted", "current", "daily", "accepted"),
        ("candidate-hidden", "current", "payroll", "pending"),
        ("candidate-uncollected", "current", "ghost", "pending"),
    ):
        session.add(
            CandidateEdge(
                id=candidate_id,
                job_id=job_id,
                origin="model",
                source_ref="c.dw.orders",
                target_ref=f"c.dw.{target}",
                relation="value_direct",
                confidence=0.4,
                status=status,
            )
        )
    session.commit()


@pytest.fixture
def health_api():
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
                # 已替换记录与当前记录共用来源键。
                index.dialect_options["sqlite"]["where"] = index.dialect_options["postgresql"]["where"]
    metadata.create_all(engine)
    app = FastAPI()
    app.include_router(catalog.router)

    @app.exception_handler(GovernanceError)
    async def governance_error(_: Request, exc: GovernanceError):
        return JSONResponse(status_code=exc.status_code, content={"detail": translate(exc.message), **exc.detail})

    with Session(engine, expire_on_commit=False) as session:
        user = User(id="viewer", username="viewer", display_name="查询者", password_hash="unused")
        _seed_snapshot(session, user)
        app.dependency_overrides[current_user] = lambda: user
        app.dependency_overrides[get_session] = lambda: session
        with TestClient(app) as client:
            yield client, session, user
    engine.dispose()


def test_load_snapshot_filters_assets_edges_columns_and_candidates(health_api):
    _, session, user = health_api
    edges, assets, candidates = lineage_health.load_snapshot(session, user, None)

    assert set(assets) == {"orders", "daily", "archive"}
    assert assets["orders"].qualified == "c.dw.orders"
    assert assets["orders"].columns == frozenset({"amount"})
    assert assets["orders"].parse_status is None
    assert (assets["daily"].parse_status, assets["daily"].parse_reason) == ("partial", PARSE_REASON)
    assert {edge.id for edge in edges} == {"e-orders", "e-archive"}
    assert [candidate.id for candidate in candidates] == ["candidate-current"]


def test_uncollected_candidate_targets_only_for_import_readers(health_api):
    _, session, user = health_api
    # 查询者：目标未采集（没有资产记录）的候选不可见
    assert "candidate-uncollected" not in {c.id for c in lineage_health.load_snapshot(session, user, None)[2]}

    session.add(UserRole(user_id=user.id, role="steward"))
    session.commit()
    ids = {c.id for c in lineage_health.load_snapshot(session, user, None)[2]}
    assert ids == {"candidate-current", "candidate-uncollected"}


def test_unbound_import_sources_rebind_to_later_collected_assets(health_api):
    _, session, user = health_api
    for edge_id, source_ref, column in (
        ("e-late", "c.dw.orders", "gone"),  # 导入后才采集到的可读来源
        ("e-late-hidden", "c.dw.payroll", "salary"),  # 补绑到不可读资产 → 整条剔除
        ("e-never", "c.dw.ghost", "x"),  # 仍未采集
    ):
        session.add(
            LineageEdge(
                id=edge_id,
                source_asset_id=None,
                source_ref=source_ref,
                source_column=column,
                target_asset_id="daily",
                target_column="total",
                relation="value_direct",
                origin="import",
            )
        )
    session.commit()

    edges, assets, candidates = lineage_health.load_snapshot(session, user, None)
    by_id = {edge.id: edge for edge in edges}
    assert by_id["e-late"].source_asset_id == "orders"
    assert "e-late-hidden" not in by_id
    assert by_id["e-never"].source_asset_id is None

    result = lineage_health.findings(edges, assets, candidates)
    uncollected = {f["subject"] for f in result["findings"] if f["code"] == "uncollected_source"}
    missing = {f["subject"] for f in result["findings"] if f["code"] == "missing_column"}
    assert uncollected == {"c.dw.ghost"}
    assert "c.dw.orders.gone" in missing


def test_get_health_all_and_asset_scope(health_api):
    client, session, _ = health_api
    response = client.get(HEALTH_URL)
    assert response.status_code == 200, response.text
    assert response.json()["scope"] == "all"
    assert response.json()["summary"] == {
        "missing_column": 2,
        "definition_unparsed": 1,
        "pending_candidates": 1,
        "low_confidence": 1,
    }

    response = client.get(HEALTH_URL, params={"asset_id": "orders"})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "scope": "asset",
        "summary": {"missing_column": 1},
        "truncated": False,
        "findings": [
            {
                "code": "missing_column",
                "severity": "danger",
                "asset_id": "orders",
                "subject": "c.dw.orders.legacy_amount",
                "detail": {"edge_id": "e-orders", "side": "source", "column": "legacy_amount", "edges": 1},
            }
        ],
    }
    assert session.scalars(select(AuditEvent)).all() == []


@pytest.mark.parametrize("asset_id", ["missing", "payroll"])
def test_health_returns_404_for_missing_or_unreadable_asset(health_api, asset_id):
    client, _, _ = health_api
    assert client.get(HEALTH_URL, params={"asset_id": asset_id}).status_code == 404
    assert client.post(REVIEW_URL, json={"asset_id": asset_id}).status_code == 404


def test_review_skips_when_policy_is_disabled(health_api, monkeypatch):
    client, session, _ = health_api
    session.get(AppSetting, modelcfg.POLICY_KEY).value = {"send_code": False}
    session.commit()
    resolve = Mock(side_effect=AssertionError("策略关闭时不应解析模型"))
    monkeypatch.setattr(modelcfg, "resolve", resolve)

    response = client.post(REVIEW_URL, json={"asset_id": "daily"})

    assert response.status_code == 200, response.text
    assert response.json()["skipped"] == "policy"
    resolve.assert_not_called()
    assert session.scalars(select(AuditEvent)).all() == []


def test_review_skips_when_no_model_is_assigned(health_api):
    client, session, _ = health_api
    response = client.post(REVIEW_URL, json={"asset_id": "daily"})

    assert response.status_code == 200, response.text
    assert response.json()["skipped"] == "no_model"
    assert session.scalars(select(AuditEvent)).all() == []


def test_review_returns_model_verdict_and_audits_metadata_only_call(health_api, monkeypatch):
    client, session, user = health_api
    verdict = {
        "verdict": "suspect",
        "summary": "血缘引用了已删除字段。",
        "items": [{"code": "missing_column", "subject": "c.dw.orders.legacy_amount", "note": "请核对当前字段。"}],
    }
    model = Mock()
    model.invoke.return_value = SimpleNamespace(content=json.dumps(verdict, ensure_ascii=False))
    resolve = Mock(return_value=SimpleNamespace(model=model))
    monkeypatch.setattr(modelcfg, "resolve", resolve)
    checked = client.get(HEALTH_URL, params={"asset_id": "daily"})
    assert checked.status_code == 200, checked.text

    response = client.post(REVIEW_URL, json={"asset_id": "daily"})

    assert response.status_code == 200, response.text
    assert response.json() == {**verdict, "findings": checked.json(), "skipped": None}
    resolve.assert_called_once_with(session, "explain")
    model.invoke.assert_called_once()
    (messages,) = model.invoke.call_args.args
    system, human = messages
    assert "有 danger 级发现时不得给 ok" in system.content
    assert EXPRESSION not in human.content and "RAW_PARSE_SQL" not in human.content
    assert "payroll" not in human.content
    payload = json.loads(human.content)
    assert payload["table"] == "c.dw.daily"
    assert payload["columns"] == ["total"]
    assert {
        "source": "c.dw.orders",
        "source_column": "legacy_amount",
        "target": "c.dw.daily",
        "target_column": "total",
        "relation": "value_direct",
    } in payload["edges"]
    assert len(payload["edges"]) == 2
    assert payload["findings"]["summary"] == checked.json()["summary"]
    (event,) = session.scalars(select(AuditEvent)).all()
    assert (event.action, event.actor_id, event.object_type, event.object_id) == (
        "lineage_health.review",
        user.id,
        "asset",
        "daily",
    )
    assert event.detail["asset_id"] == "daily"
    assert event.detail["verdict"] == "suspect"
    assert event.detail["finding_count"] == 5


@pytest.mark.parametrize(
    "reply",
    [
        "这不是 JSON",
        '{"verdict": "yes", "summary": "无法判定", "items": []}',
        '{"verdict": "unknown", "summary": "无法判定", "items": [{"code": "missing_column"}]}',
    ],
)
def test_review_returns_422_for_unusable_output(health_api, monkeypatch, reply):
    client, session, _ = health_api
    model = Mock()
    model.invoke.return_value = SimpleNamespace(content=reply)
    monkeypatch.setattr(modelcfg, "resolve", Mock(return_value=SimpleNamespace(model=model)))

    response = client.post(REVIEW_URL, json={"asset_id": "daily"})

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == INVALID_REVIEW
    assert session.scalars(select(AuditEvent)).all() == []


def test_review_returns_422_for_model_failure(health_api, monkeypatch):
    client, session, _ = health_api
    error = TimeoutError("上游模型超时")
    model = Mock()
    model.invoke.side_effect = error
    safe_error = Mock(return_value="已脱敏的错误")
    monkeypatch.setattr(modelcfg, "resolve", Mock(return_value=SimpleNamespace(model=model, safe_error=safe_error)))

    response = client.post(REVIEW_URL, json={"asset_id": "daily"})

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == INVALID_REVIEW
    safe_error.assert_called_once_with(error)
    assert session.scalars(select(AuditEvent)).all() == []


def test_review_downgrades_ok_when_danger_findings_exist(health_api, monkeypatch):
    """有严重级发现时，模型即使给 ok，服务端也改为 suspect。"""
    client, _, _ = health_api
    model = Mock()
    model.invoke.return_value = SimpleNamespace(
        content=json.dumps({"verdict": "ok", "summary": "没问题。", "items": []})
    )
    monkeypatch.setattr(modelcfg, "resolve", Mock(return_value=SimpleNamespace(model=model)))
    checked = client.get(HEALTH_URL, params={"asset_id": "orders"}).json()
    assert any(item["severity"] == "danger" for item in checked["findings"])
    response = client.post(REVIEW_URL, json={"asset_id": "orders"})
    assert response.status_code == 200, response.text
    assert response.json()["verdict"] == "suspect"
