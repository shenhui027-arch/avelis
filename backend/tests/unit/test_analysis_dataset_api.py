"""数据集详情、撤权与重跑的 HTTP 合约；内存 SQLite，不连接外部服务。"""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import JSON, MetaData, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from avelis.api import analysis
from avelis.api.deps import current_user, get_session
from avelis.ask import service as ask
from avelis.db import Base, utcnow
from avelis.governance.errors import GovernanceError
from avelis.models import (
    AnalysisDataset,
    AnalysisRun,
    AnalysisStep,
    Asset,
    Datasource,
    Domain,
    DomainMember,
    Metric,
    MetricVersion,
    User,
    UserRole,
)


@pytest.fixture
def api():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for source in Base.metadata.tables.values():
        table = source.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(engine)
    app = FastAPI()
    app.include_router(analysis.router)

    @app.exception_handler(GovernanceError)
    async def governance_error(_: Request, exc: GovernanceError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message, **exc.detail})

    with Session(engine, expire_on_commit=False) as session:
        user = User(id="actor", username="actor", display_name="分析用户", password_hash="unused")
        session.add_all(
            [
                user,
                UserRole(user_id=user.id, role="analyst"),
                Domain(id="asset-domain", code="assets", name="来源域", owner_id=user.id),
                Domain(id="metric-domain", code="metrics", name="指标域", owner_id=user.id),
                DomainMember(domain_id="asset-domain", user_id=user.id, role="viewer"),
                DomainMember(domain_id="metric-domain", user_id=user.id, role="viewer"),
                Datasource(id="source", name="offline", kind="fake", host="offline", port=1),
            ]
        )
        asset = Asset(
            id="asset",
            datasource_id="source",
            kind="table",
            catalog_name="local",
            db_name="data",
            name="facts",
            domain_id="asset-domain",
        )
        metric = Metric(
            id="metric", code="measure", name="指标", domain_id="metric-domain", owner_id=user.id, created_by=user.id
        )
        version = MetricVersion(
            id="version",
            metric_id=metric.id,
            version_no=1,
            status="published",
            kind="base",
            base_asset_id=asset.id,
            description="测试口径",
            expression="SUM(value)",
            time_column="day",
            timezone="Asia/Shanghai",
            unit="件",
            created_by=user.id,
        )
        run = AnalysisRun(
            id="run",
            user_id=user.id,
            question="检查指标变化",
            trigger="manual",
            status="succeeded",
            report={"title": "原报告"},
            verification={"ok": True},
            model_adapter="offline",
        )
        intent = {
            "metric_code": metric.code,
            "metric_codes": [metric.code],
            "start_date": "2026-01-01",
            "end_date": "2026-01-03",
            "compare": "previous_period",
        }
        dataset = AnalysisDataset(
            id="dataset",
            run_id=run.id,
            seq=1,
            kind="governed",
            title="指标 2026-01-01~2026-01-03",
            columns=["period", "指标"],
            rows=[[f"row-{i}", i] for i in range(1205)],
            truncated=True,
            meta={
                "sql": "SELECT day, SUM(value) FROM facts GROUP BY day",
                "metric_version_ids": [version.id],
                "metric_codes": [metric.code],
                "intent": intent,
                "window": {"start": "2026-01-01", "end": "2026-01-03"},
                "time_column": "period",
                "units": {"指标": {"kind": "level", "unit": "件"}},
                "summary_units": {"total": {"kind": "level", "unit": "件"}},
                "purpose": "检查变化",
                "explore": False,
                "assumption": False,
                "partial": False,
            },
        )
        session.add_all([asset, metric, version, run, dataset])
        session.commit()
        app.dependency_overrides[current_user] = lambda: user
        app.dependency_overrides[get_session] = lambda: session
        with TestClient(app) as client:
            yield SimpleNamespace(
                client=client,
                app=app,
                session=session,
                user=user,
                run=run,
                dataset=dataset,
                asset=asset,
                metric=metric,
                version=version,
            )
    engine.dispose()


def test_detail_fields_default_page_and_legacy_full_rows(api):
    response = api.client.get("/api/analysis/runs/run/datasets/D1")
    assert response.status_code == 200, response.text
    data = response.json()
    chart_specs = data["charts"]
    assert chart_specs[0]["kind"] == "line"
    assert len(chart_specs[0]["categories"]) == len(api.dataset.rows)
    assert data == {
        "ref": "D1",
        "kind": "governed",
        "title": api.dataset.title,
        "columns": ["period", "指标"],
        "row_count": 1205,
        "truncated": True,
        "inputs": [],
        "sql": api.dataset.meta["sql"],
        "metric_version_ids": ["version"],
        "metric_codes": ["measure"],
        "window": {"start": "2026-01-01", "end": "2026-01-03"},
        "time_column": "period",
        "units": {"指标": {"kind": "level", "unit": "件", "additive": True}},
        "polarity": {},
        "column_roles": {
            "period": {"role": "time", "label": "period", "grain": None, "additive": None},
            "指标": {"role": "measure", "label": "指标·本期", "grain": None, "additive": True},
        },
        "charts": chart_specs,
        "summary_units": {"total": {"kind": "level", "unit": "件"}},
        "purpose": "检查变化",
        "answer_id": None,
        "created_at": api.dataset.created_at.isoformat(),
        "explore": False,
        "assumption": False,
        "partial": False,
        "rerunnable": True,
        "source": None,
        "meta": {
            **api.dataset.meta,
            "units": {"指标": {"kind": "level", "unit": "件", "additive": True}},
            "polarity": {},
            "dimension_labels": {},
            "measure_labels": {"指标": "指标"},
            "column_labels": {"指标": "指标·本期"},
        },
        "offset": 0,
        "limit": 100,
        "rows": api.dataset.rows[:100],
        "total": 1205,
    }
    response = api.client.get("/api/analysis/runs/run/datasets/1?offset=100&limit=1")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "ref": "D1",
        "kind": "governed",
        "title": api.dataset.title,
        "columns": api.dataset.columns,
        "rows": api.dataset.rows,
        "truncated": True,
        "meta": {
            **api.dataset.meta,
            "units": {"指标": {"kind": "level", "unit": "件", "additive": True}},
            "polarity": {},
            "dimension_labels": {},
            "measure_labels": {"指标": "指标"},
            "column_labels": {"指标": "指标·本期"},
        },
        "inputs": [],
        "answer_id": None,
        "column_roles": {
            "period": {"role": "time", "label": "period", "grain": None, "additive": None},
            "指标": {"role": "measure", "label": "指标·本期", "grain": None, "additive": True},
        },
    }
    view = api.client.get("/api/analysis/runs/run").json()
    assert view["datasets"][0]["rows"] == api.dataset.rows[:50]
    assert view["datasets"][0]["charts"] == chart_specs
    assert "charts" not in api.dataset.meta


@pytest.mark.parametrize("offset,limit,size", [(100, 1000, 1000), (1200, 100, 5), (2000, 100, 0)])
def test_detail_paginates_saved_rows(api, offset, limit, size):
    response = api.client.get(f"/api/analysis/runs/run/datasets/D1?offset={offset}&limit={limit}")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["rows"] == api.dataset.rows[offset : offset + limit]
    assert len(data["rows"]) == size
    assert (data["offset"], data["limit"], data["total"]) == (offset, limit, 1205)
    assert data["charts"][0]["kind"] == "line"
    assert len(data["charts"][0]["categories"]) == 1205


@pytest.mark.parametrize("query", ["offset=-1", "limit=0", "limit=1001"])
def test_detail_rejects_invalid_page(api, query):
    assert api.client.get(f"/api/analysis/runs/run/datasets/D1?{query}").status_code == 422


@pytest.mark.parametrize("ref", ["D0", "D01", "d1", "nope", "D999"])
def test_detail_invalid_or_unknown_ref_is_not_found(api, ref):
    assert api.client.get(f"/api/analysis/runs/run/datasets/{ref}").status_code == 404


def test_detail_exposes_source_and_stored_provenance_flags(api):
    api.dataset.meta = {
        **api.dataset.meta,
        "rerun_of": {"run_id": "origin", "ref": "D3", "dataset_id": "original"},
        "explore": True,
        "assumption": True,
        "partial": True,
    }
    api.session.commit()
    response = api.client.get("/api/analysis/runs/run/datasets/D1")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["source"] == {"run_id": "origin", "ref": "D3"}
    assert data["explore"] and data["assumption"] and data["partial"]
    api.dataset.meta = {key: value for key, value in api.dataset.meta.items() if key != "intent"}
    api.session.commit()
    assert api.client.get("/api/analysis/runs/run/datasets/D1").json()["rerunnable"] is False


@pytest.mark.parametrize("domain", ["asset-domain", "metric-domain"])
def test_current_domain_membership_revocation_restricts_all_http_reads_and_calc_inputs(api, domain):
    secret = "private-row-marker"
    api.dataset.rows = [[secret, 10], ["other-row", 20]]
    api.run.report = {**api.run.report, "claims": [{"type": "fact", "text": "指标明细见数据。", "refs": ["D1"]}]}
    calc = AnalysisDataset(
        id="calc",
        run_id=api.run.id,
        seq=2,
        kind="calc",
        title="计算结果",
        inputs=["D1"],
        columns=["derived"],
        rows=[["private-calculation-marker"]],
        meta={"closure": ["D1"]},
    )
    api.session.add_all(
        [
            calc,
            AnalysisStep(
                run_id=api.run.id,
                seq=1,
                kind="tool",
                tool="query_metric",
                status="ok",
                dataset_id=api.dataset.id,
                output={"rows": api.dataset.rows, "dataset": "D1"},
            ),
        ]
    )
    api.session.commit()
    assert api.client.get("/api/analysis/runs/run/datasets/D1").status_code == 200
    assert api.client.get("/api/analysis/runs/run/datasets/D2").status_code == 200
    assert secret in api.client.get("/api/analysis/runs/run/export?format=md").text
    membership = api.session.scalar(
        select(DomainMember).where(DomainMember.user_id == api.user.id, DomainMember.domain_id == domain)
    )
    api.session.delete(membership)
    api.session.commit()

    for suffix in ("1", "1/csv", "D1", "2", "2/csv", "D2"):
        response = api.client.get(f"/api/analysis/runs/run/datasets/{suffix}")
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == "你已没有该数据的访问权限"
    response = api.client.get("/api/analysis/runs/run")
    assert response.status_code == 200, response.text
    view = response.json()
    assert all(dataset["restricted"] and dataset["rows"] == [] for dataset in view["datasets"])
    assert view["steps"][0]["output"]["restricted"] is True
    assert view["steps"][0]["output"]["rows"] == []
    response = api.client.get("/api/analysis/runs/run/export?format=md")
    assert response.status_code == 200, response.text
    assert secret not in response.text and "private-calculation-marker" not in response.text
    response = api.client.post("/api/analysis/runs/run/datasets/D1/rerun", json={"window": WINDOW})
    assert response.status_code == 403, response.text


WINDOW = {"start": "2026-02-10", "end": "2026-02-12"}


def test_rerun_http_returns_paginated_detail_and_persists_source_and_step(api, monkeypatch):
    catalog = ask.Catalog([], {api.metric.code: (api.metric, api.version)})
    calls = []

    def visible_catalog(session, user, *, with_values):
        assert session is api.session and user is api.user and with_values is False
        calls.append("catalog")
        return catalog

    def fulfil(session, user, intent, answer, visible):
        assert session is api.session and user is api.user and visible is catalog
        calls.append("fulfil")
        assert str(intent.compare_start) == "2026-02-07" and str(intent.compare_end) == "2026-02-09"
        answer.status = "answered"
        answer.result = {"columns": ["value"], "rows": [[i] for i in range(105)], "truncated": False}
        answer.sql = "SELECT SUM(value) FROM facts"
        answer.metric_version_ids = [api.version.id]
        answer.cache_hit = True
        answer.executed_at = utcnow()

    monkeypatch.setattr(ask, "visible_catalog", visible_catalog)
    monkeypatch.setattr(ask, "fulfil", fulfil)
    monkeypatch.setattr(ask.audit, "record", lambda *args, **kwargs: None)
    response = api.client.post("/api/analysis/runs/run/datasets/D1/rerun", json={"window": WINDOW})
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["ref"] == "D2" and detail["kind"] == "governed"
    assert detail["title"] == "指标 2026-02-10~2026-02-12"
    assert detail["columns"] == ["指标"] and detail["rows"] == [[i] for i in range(100)]
    assert (detail["offset"], detail["limit"], detail["row_count"], detail["total"]) == (0, 100, 105, 105)
    assert detail["source"] == {"run_id": "run", "ref": "D1"}
    assert detail["window"] == WINDOW and detail["rerunnable"] is True
    assert detail["meta"]["rerun_of"] == {"run_id": "run", "ref": "D1", "dataset_id": "dataset"}
    assert detail["meta"]["original_intent"] == api.dataset.meta["intent"]
    assert detail["meta"]["cached"] is True and detail["meta"]["executed_at"]
    assert detail["metric_version_ids"] == [api.version.id]
    assert detail["answer_id"] and detail["created_at"]
    assert calls == ["catalog", "fulfil"]
    api.session.expire_all()
    retrieved = api.client.get("/api/analysis/runs/run/datasets/D2").json()
    assert retrieved.pop("created_at").removesuffix("+00:00") == detail["created_at"].removesuffix("+00:00")
    assert retrieved == {key: value for key, value in detail.items() if key != "created_at"}
    stored = api.session.scalar(select(AnalysisDataset).where(AnalysisDataset.seq == 2))
    assert stored.rows == [[i] for i in range(105)]
    step = api.session.scalar(select(AnalysisStep))
    assert step.kind == "tool" and step.tool == "rerun" and step.dataset_id == stored.id
    assert step.output["dataset"] == "D2"
    assert api.run.report == {"title": "原报告"} and api.run.verification == {"ok": True}
    assert api.run.status == "succeeded" and len(api.dataset.rows) == 1205


@pytest.mark.parametrize("admin", [False, True])
def test_only_owner_can_rerun_through_http_even_when_admin_can_read(api, admin):
    other = User(id="other", username="other", display_name="其他用户", password_hash="unused")
    api.session.add(other)
    if admin:
        api.session.add(UserRole(user_id=other.id, role="admin"))
    api.session.commit()
    api.app.dependency_overrides[current_user] = lambda: other
    response = api.client.post("/api/analysis/runs/run/datasets/D1/rerun", json={"window": WINDOW})
    assert response.status_code in (403, 404), response.text
    assert len(list(api.session.scalars(select(AnalysisDataset)))) == 1


@pytest.mark.parametrize("kind,intent", [("calc", True), ("governed", False)])
def test_rerun_requires_governed_dataset_with_intent(api, kind, intent):
    api.dataset.kind = kind
    if not intent:
        api.dataset.meta = {key: value for key, value in api.dataset.meta.items() if key != "intent"}
    api.session.commit()
    response = api.client.post("/api/analysis/runs/run/datasets/D1/rerun", json={"window": WINDOW})
    assert response.status_code == 409, response.text


@pytest.mark.parametrize("status", ["queued", "running", "waiting"])
def test_rerun_rejects_busy_run(api, status):
    api.run.status = status
    api.session.commit()
    response = api.client.post("/api/analysis/runs/run/datasets/D1/rerun", json={"window": WINDOW})
    assert response.status_code == 409, response.text


def test_custom_rerun_requires_new_comparison_dates(api):
    api.dataset.meta = {
        **api.dataset.meta,
        "intent": {
            **api.dataset.meta["intent"],
            "compare": "custom",
            "compare_start": "2025-12-01",
            "compare_end": "2025-12-03",
        },
    }
    api.session.commit()
    response = api.client.post("/api/analysis/runs/run/datasets/D1/rerun", json={"window": WINDOW})
    assert response.status_code == 422, response.text
    assert len(list(api.session.scalars(select(AnalysisDataset)))) == 1


@pytest.mark.parametrize(
    "window",
    [
        {"start": "invalid", "end": "2026-02-12"},
        {"start": "2026-02-13", "end": "2026-02-12"},
        {**WINDOW, "compare_start": "2026-01-01"},
    ],
)
def test_rerun_window_request_validation(api, window):
    response = api.client.post("/api/analysis/runs/run/datasets/D1/rerun", json={"window": window})
    assert response.status_code == 422, response.text


def test_dataset_openapi_exports_paths_and_named_contracts(api):
    response = api.client.get("/openapi.json")
    assert response.status_code == 200, response.text
    document = response.json()
    paths = document["paths"]
    detail = paths["/api/analysis/runs/{run_id}/datasets/{ref}"]["get"]
    assert {parameter["name"] for parameter in detail["parameters"]} == {"run_id", "ref", "offset", "limit"}
    limit = next(parameter["schema"] for parameter in detail["parameters"] if parameter["name"] == "limit")
    assert limit["default"] == 100 and limit["maximum"] == 1000
    rerun = paths["/api/analysis/runs/{run_id}/datasets/{ref}/rerun"]["post"]
    assert rerun["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/AnalysisDatasetRerunIn")
    schemas = document["components"]["schemas"]
    assert {
        "AnalysisDatasetDetailOut",
        "AnalysisDatasetLegacyOut",
        "AnalysisDatasetRerunIn",
        "AnalysisDatasetRerunWindow",
        "AnalysisDatasetSource",
    } <= schemas.keys()
    assert {"ref", "rows", "total", "source", "rerunnable", "units", "metric_codes"} <= schemas[
        "AnalysisDatasetDetailOut"
    ]["properties"].keys()
    json.dumps(document, ensure_ascii=False)


def test_revoked_report_values_and_sql_are_hidden_in_views_and_exports_without_rewriting_report(api):
    secret = "987654321.123456"
    segment = {
        "kind": "value",
        "ref": "D1",
        "part": 2,
        "raw": secret,
        "text": secret,
        "path": f"value {secret}",
        "label": "指标",
        "measure": "level",
        "unit": "件",
    }
    record = {
        "text": "结果 " + secret,
        "segments": [{"kind": "text", "text": "结果 "}, segment],
        "refs": ["D1"],
        "errors": [],
        "status": "verified",
    }
    api.run.report = {
        "title": record["text"],
        "summary": record["text"],
        "fields": {"summary": record, "title": deepcopy(record)},
        "claims": [{**deepcopy(record), "type": "fact", "details": {}}],
    }
    api.dataset.meta = {**api.dataset.meta, "sql": f"SELECT {secret}", "summary": {"total": secret}}
    api.session.add(
        AnalysisStep(
            run_id=api.run.id,
            seq=1,
            kind="tool",
            tool="query_sql",
            status="ok",
            dataset_id=api.dataset.id,
            args={"sql": api.dataset.meta["sql"]},
            output={"nested": {"summary": secret}, "rows": [[secret]]},
        )
    )
    api.session.add(
        AnalysisStep(
            run_id=api.run.id,
            seq=2,
            kind="note",
            tool="submit_report",
            status="ok",
            args={"title": record["text"]},
            output={"claims": 1},
        )
    )
    api.session.commit()
    original = deepcopy(api.run.report)
    assert secret in api.client.get("/api/analysis/runs/run/export?format=csv").text
    membership = api.session.scalar(
        select(DomainMember).where(DomainMember.user_id == api.user.id, DomainMember.domain_id == "asset-domain")
    )
    api.session.delete(membership)
    api.session.commit()

    for suffix in ("", "/export?format=csv", "/export?format=md"):
        response = api.client.get("/api/analysis/runs/run" + suffix)
        assert response.status_code == 200, response.text
        assert secret not in response.text
    assert secret not in api.client.get("/api/analysis/runs").text
    detail = api.client.get("/api/analysis/runs/run").json()
    assert detail["datasets"][0]["summary"] is None and detail["datasets"][0]["sql"] is None
    assert detail["steps"][0]["args"] == detail["steps"][1]["args"] == {}
    assert detail["steps"][0]["output"] == {"rows": [], "restricted": True}
    api.session.refresh(api.run)
    assert api.run.report == original and api.run.verification == {"ok": True}
