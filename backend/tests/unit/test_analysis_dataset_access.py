"""历史数据集撤权：真实 SQLite、来源闭包与 HTTP 读取，不连接外部服务。"""

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import JSON, MetaData, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from avelis.analysis import service
from avelis.analysis.dataset_access import can_read_dataset, require_dataset_access
from avelis.api import analysis
from avelis.api.deps import current_user, get_session
from avelis.db import Base
from avelis.governance.errors import Forbidden, GovernanceError
from avelis.models import (
    AnalysisDataset, AnalysisRun, AnalysisStep, Answer, Asset, Datasource, Domain, DomainMember,
    IdentityLink, LineageEdge, Metric, MetricVersion, User, UserRole,
)


@pytest.fixture
def dataset_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for source in Base.metadata.tables.values():
        table = source.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        user = User(id="reader", username="reader", display_name="阅读者", password_hash="unused")
        session.add_all([user, UserRole(user_id=user.id, role="analyst"),
                         Domain(id="visible", code="visible", name="可见域", owner_id=user.id),
                         Domain(id="hidden", code="hidden", name="其他域", owner_id=user.id),
                         DomainMember(domain_id="visible", user_id=user.id, role="viewer"),
                         Datasource(id="source", name="source", kind="fake", host="offline", port=1)])
        asset = Asset(id="asset", datasource_id="source", kind="table", catalog_name="local", db_name="data",
                      name="facts", domain_id="visible")
        metric = Metric(id="metric", code="m", name="指标", domain_id="visible", owner_id=user.id, created_by=user.id)
        version = MetricVersion(id="version", metric_id=metric.id, version_no=1, status="published", kind="base",
                                base_asset_id=asset.id, description="测试口径", expression="SUM(value)",
                                time_column="day", timezone="Asia/Shanghai", created_by=user.id)
        run = AnalysisRun(id="run", user_id=user.id, question="查看数据", trigger="manual", status="succeeded",
                          window={"start": "2026-01-01", "end": "2026-01-31"}, model_adapter="offline")
        dataset = AnalysisDataset(id="dataset", run_id=run.id, seq=1, kind="governed", title="指标数据",
                                  columns=["value"], rows=[[7], [9]], meta={"metric_version_ids": [version.id]})
        session.add_all([asset, metric, version, run, dataset])
        session.commit()
        yield session, user, run, dataset, asset, metric, version
    engine.dispose()


@pytest.mark.parametrize("revoked", ["asset", "metric", "membership"])
def test_revocation_restricts_governed_calc_run_and_step_rows(dataset_db, revoked):
    session, user, run, dataset, asset, metric, _ = dataset_db
    calc = AnalysisDataset(id="calc", run_id=run.id, seq=2, kind="calc", title="计算结果", inputs=["D1"],
                           columns=["value"], rows=[[16]], meta={"closure": ["D1"], "summary": {"total": 16}})
    dataset.meta = {**dataset.meta, "sql": "SELECT value FROM local.data.facts"}
    step = AnalysisStep(run_id=run.id, seq=1, kind="tool", tool="query_metric", status="ok", dataset_id=dataset.id,
                        args={"sql": dataset.meta["sql"]},
                        output={"dataset": "D1", "columns": dataset.columns, "rows": dataset.rows,
                                "summary": {"total": 16}})
    session.add_all([calc, step])
    session.commit()
    assert can_read_dataset(session, user, dataset)
    assert can_read_dataset(session, user, calc)
    if revoked == "membership":
        session.query(DomainMember).filter_by(user_id=user.id).delete()
    else:
        (asset if revoked == "asset" else metric).domain_id = "hidden"
    session.commit()

    with pytest.raises(Forbidden, match="你已没有该数据的访问权限"):
        require_dataset_access(session, user, dataset)
    view = service.run_view(session, run, user=user)
    assert all(d["restricted"] and d["rows"] == [] for d in view["datasets"])
    assert all(d["summary"] is None and d["sql"] is None for d in view["datasets"])
    assert view["steps"][0]["output"] == {"rows": [], "restricted": True}
    assert view["steps"][0]["args"]["sql"] is None
    assert service.run_view(session, run)["datasets"][0]["rows"] == [[7], [9]]


def test_derived_components_and_historical_versions_are_checked(dataset_db):
    session, user, _, dataset, asset, metric, version = dataset_db
    derived = Metric(id="derived", code="derived", name="组合指标", domain_id="visible",
                     owner_id=user.id, created_by=user.id)
    dv = MetricVersion(id="derived-version", metric_id=derived.id, version_no=1, status="published", kind="derived",
                       components=[{"metric_id": metric.id, "version_id": version.id}], description="组合口径",
                       expression="m * 2", time_column="day", timezone="Asia/Shanghai", created_by=user.id)
    session.add_all([derived, dv])
    dataset.meta = {"metric_version_ids": [dv.id]}
    session.commit()
    assert can_read_dataset(session, user, dataset)
    metric.domain_id = "hidden"
    session.commit()
    assert not can_read_dataset(session, user, dataset)
    metric.domain_id = "visible"
    replacement = Asset(id="new-source", datasource_id="source", kind="table", catalog_name="local",
                        db_name="data", name="new_facts", domain_id="visible")
    current = MetricVersion(id="new-version", metric_id=metric.id, version_no=2, status="published", kind="base",
                            base_asset_id=replacement.id, description="新口径", expression="SUM(value)",
                            time_column="day", timezone="Asia/Shanghai", created_by=user.id)
    session.add_all([replacement, current])
    asset.domain_id = "hidden"
    session.commit()
    assert not can_read_dataset(session, user, dataset)  # 最新版本可见不能替代旧版本的来源权限


@pytest.mark.parametrize("link", ["lineage", "identity"])
def test_upstream_sources_are_rechecked(dataset_db, link):
    session, user, _, dataset, asset, _, _ = dataset_db
    upstream = Asset(id="upstream", datasource_id="source", kind="table", catalog_name="local", db_name="data",
                     name="upstream", domain_id="visible")
    session.add(upstream)
    if link == "lineage":
        session.add(LineageEdge(origin="manual", target_asset_id=asset.id, source_asset_id=upstream.id,
                                source_ref="local.data.upstream", relation="value"))
    else:
        session.add(IdentityLink(source_asset_id=asset.id, target_asset_id=upstream.id,
                                 status="confirmed", method="manual"))
    session.commit()
    assert can_read_dataset(session, user, dataset)
    upstream.domain_id = "hidden"
    session.commit()
    assert not can_read_dataset(session, user, dataset)


@pytest.mark.parametrize("kind,meta,columns,rows", [
    ("explore", {"tables": [{"asset_id": "asset"}]}, ["value"], [[7]]),
    ("context", {"asset_ids": ["asset"]}, ["value"], [[7]]),
    ("context", {}, ["base_asset"], [["local.data.facts"]]),
    ("context", {}, ["type", "object"], [["元数据变更", "local.data.facts"]]),
])
def test_explore_and_current_and_legacy_context_sources(dataset_db, kind, meta, columns, rows):
    session, user, _, dataset, asset, _, _ = dataset_db
    dataset.kind, dataset.meta, dataset.columns, dataset.rows = kind, meta, columns, rows
    session.commit()
    assert can_read_dataset(session, user, dataset)
    asset.domain_id = "hidden"
    session.commit()
    assert not can_read_dataset(session, user, dataset)


def test_lookup_answer_is_withheld_after_source_revocation(dataset_db):
    session, user, run, _, asset, _, version = dataset_db
    answer = Answer(id="answer", question="查询", asked_by=user.id, status="answered", model_adapter="offline",
                    metric_version_ids=[version.id], result={"columns": ["value"], "rows": [[7]]})
    session.add(answer)
    run.report = {"kind": "lookup", "answer_id": answer.id}
    session.commit()
    assert service.run_view(session, run, user=user)["answer"]["result"]["rows"] == [[7]]
    asset.domain_id = "hidden"
    session.commit()
    assert "answer" not in service.run_view(session, run, user=user)


def test_answer_keeps_historical_actual_source_checks(dataset_db):
    session, user, _, dataset, _, _, version = dataset_db
    former_source = Asset(id="former-source", datasource_id="source", kind="table", catalog_name="local",
                          db_name="data", name="former_source", domain_id="visible")
    answer = Answer(id="saved-answer", question="查询", asked_by=user.id, status="answered", model_adapter="offline",
                    metric_version_ids=[version.id], evidence={"sources": [{"asset_id": former_source.id}]})
    session.add_all([former_source, answer])
    dataset.answer_id = answer.id
    session.commit()
    assert can_read_dataset(session, user, dataset)
    former_source.domain_id = "hidden"
    session.commit()
    assert not can_read_dataset(session, user, dataset)


def test_nonempty_context_with_no_traceable_source_is_restricted(dataset_db):
    session, user, _, dataset, _, _, _ = dataset_db
    dataset.kind = "context"
    dataset.meta = {"asset_ids": [], "metric_version_ids": []}
    session.commit()
    assert not can_read_dataset(session, user, dataset)


def test_dataset_http_reads_return_forbidden_after_revocation(dataset_db):
    session, user, run, _, asset, _, _ = dataset_db
    app = FastAPI()
    app.include_router(analysis.router)
    app.dependency_overrides[current_user] = lambda: user
    app.dependency_overrides[get_session] = lambda: session

    @app.exception_handler(GovernanceError)
    async def governance_error(_: Request, exc: GovernanceError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    with TestClient(app) as client:
        assert client.get(f"/api/analysis/runs/{run.id}/datasets/1").status_code == 200
        asset.domain_id = "hidden"
        session.commit()
        for suffix in ("1", "1/csv", "D1"):
            response = client.get(f"/api/analysis/runs/{run.id}/datasets/{suffix}")
            assert response.status_code == 403, response.text
            assert response.json()["detail"] == "你已没有该数据的访问权限"
