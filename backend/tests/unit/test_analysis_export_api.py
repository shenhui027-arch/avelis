"""导出 HTTP 契约：真实路由、服务与内存 SQLite，不连接外部服务。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import JSON, MetaData, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from avelis.api import analysis
from avelis.api.deps import current_user, get_session
from avelis.models import (AnalysisDataset, AnalysisMessage, AnalysisRun, AnalysisStep, AnalysisTask, Asset,
                           AssetColumn, AssetVersion, CollectionRun, ColumnVersion, Datasource, Domain,
                           DomainMember, Metric, MetricVersion, User, UserRole)


@pytest.fixture
def api():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in (User, UserRole, Domain, DomainMember, Datasource, Asset, AssetColumn, CollectionRun,
                  AssetVersion, ColumnVersion, Metric, MetricVersion, AnalysisTask, AnalysisRun,
                  AnalysisStep, AnalysisDataset, AnalysisMessage):
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(engine)
    app = FastAPI()
    app.include_router(analysis.router)
    with Session(engine, expire_on_commit=False) as session:
        user = User(id="actor", username="actor", display_name="分析用户", password_hash="unused")
        report = {"title": "变化分析", "summary": "本期较对比期下降。", "claims": [
            {"type": "fact", "text": "指标存在变化。", "refs": ["D1", "D2"]}]}
        run = AnalysisRun(id="run", user_id=user.id, question="检查指标变化", trigger="manual", status="succeeded",
                          window={}, report=report, verification={"ok": True, "claims": [
                              {"index": 0, "status": "verified"}]})
        other = AnalysisRun(id="other", user_id=user.id, question="其他分析", trigger="manual", status="succeeded",
                            window={})
        session.add_all([user, run, other])
        session.flush()
        session.add_all([
            AnalysisDataset(run_id=run.id, seq=1, kind="governed", title="分组明细", columns=["分组", "指标"],
                            rows=[[f"类别 {i}", i * 1000] for i in range(60)],
                            meta={"units": {"指标": {"kind": "level", "unit": "元"}},
                                  "intent": {"group_by": ["segment"]}}),
            AnalysisDataset(run_id=run.id, seq=2, kind="governed", title="对照明细", columns=["分组", "指标"],
                            rows=[["甲", 2000], ["乙", 1000]],
                            meta={"units": {"指标": {"kind": "level", "unit": "元"}}}),
            AnalysisDataset(run_id=other.id, seq=99, kind="context", title="其他运行证据", columns=[], rows=[]),
        ])
        session.commit()
        app.dependency_overrides[current_user] = lambda: user
        app.dependency_overrides[get_session] = lambda: session
        with TestClient(app) as client:
            yield client, session, run
    engine.dispose()


def test_export_defaults_use_all_saved_rows_and_metadata(api, monkeypatch):
    client, _, run = api
    captured = {}

    def markdown(view, **kwargs):
        captured.update(view=view, kwargs=kwargs)
        return "export"

    monkeypatch.setattr(analysis.export, "to_markdown", markdown)
    response = client.get(f"/api/analysis/runs/{run.id}/export", params={"format": "md"})
    assert response.status_code == 200, response.text
    assert response.text == "export"
    assert captured["kwargs"] == {"audience": "brief", "datasets": None}
    dataset = captured["view"]["datasets"][0]
    assert len(dataset["rows"]) == dataset["row_count"] == 60
    assert dataset["rows"][-1] == ["类别 59", 59000]
    assert dataset["meta"]["units"]["指标"] == {"kind": "level", "unit": "元"}


def test_export_pdf_passes_requested_audience_and_dataset_order(api, monkeypatch):
    client, _, run = api
    captured = {}

    def pdf(view, **kwargs):
        captured.update(view=view, kwargs=kwargs)
        return b"%PDF-offline"

    monkeypatch.setattr(analysis.export, "to_pdf", pdf)
    response = client.get(f"/api/analysis/runs/{run.id}/export", params={
        "audience": "full", "datasets": "D2, D1,D2"})
    assert response.status_code == 200, response.text
    assert response.content == b"%PDF-offline"
    assert captured["kwargs"] == {"audience": "full", "datasets": ["D2", "D1"]}
    assert len(captured["view"]["datasets"][0]["rows"]) == 60


@pytest.mark.parametrize("version_meta", [
    {"metric_version_ids": ["metric-old"], "metric_codes": ["amount"]},
    {"versions": {"amount": "metric-old"}},
])
def test_export_uses_pinned_metric_dimension_labels_without_mutating_saved_meta(api, monkeypatch, version_meta):
    client, session, run = api
    metric = Metric(id="metric", code="amount", name="指标名称", domain_id="domain",
                    owner_id="actor", created_by="actor")
    session.add(metric)
    for version_id, version_no, label in [("metric-old", 1, "历史分组名称"), ("metric-new", 2, "最新分组名称")]:
        session.add(MetricVersion(id=version_id, metric_id=metric.id, version_no=version_no,
                                  status="published", description="测试口径", expression="SUM(b.value)",
                                  time_column="b.day", timezone="Asia/Shanghai", created_by="actor",
                                  dimensions=[{"name": "segment", "column": "b.segment", "label": label}]))
    dataset = session.scalar(select(AnalysisDataset).where(AnalysisDataset.run_id == run.id, AnalysisDataset.seq == 1))
    original = {**dataset.meta, **version_meta, "column_docs": {"segment": {"comment": "已有列注释"}}}
    dataset.meta = original
    session.commit()
    captured = {}

    def markdown(view, **kwargs):
        captured.update(view=view)
        return "export"

    monkeypatch.setattr(analysis.export, "to_markdown", markdown)
    response = client.get(f"/api/analysis/runs/{run.id}/export", params={"format": "md"})
    assert response.status_code == 200, response.text
    labels = captured["view"]["datasets"][0]["meta"]["export_labels"]
    assert labels == {"metric_names": ["指标名称"],
                      "dimensions": [{"name": "segment", "column": "b.segment", "label": "历史分组名称"}],
                      "column_docs": {"segment": {"comment": "已有列注释"}}}
    session.refresh(dataset)
    assert dataset.meta == original
    assert "export_labels" not in dataset.meta


@pytest.mark.parametrize("visible", [True, False])
def test_export_column_comment_uses_pinned_asset_snapshot_and_current_visibility(api, monkeypatch, visible):
    client, session, run = api
    session.add(Metric(id="metric", code="amount", name="指标名称", domain_id="domain",
                       owner_id="actor", created_by="actor"))
    session.add(Asset(id="asset", datasource_id="source", kind="table", catalog_name="c", db_name="d",
                      name="facts", domain_id="domain", current_version_id="asset-new"))
    if visible:
        session.add(DomainMember(domain_id="domain", user_id="actor", role="viewer"))
    session.add(MetricVersion(id="metric-old", metric_id="metric", version_no=1, status="published",
                              description="测试口径", expression="SUM(b.value)", time_column="b.day",
                              timezone="Asia/Shanghai", created_by="actor",
                              dimensions=[{"name": "segment", "column": "b.segment"}],
                              dependency=[{"alias": "b", "asset_id": "asset", "column": "segment",
                                           "role": "dimension"}],
                              asset_versions={"asset": {"version_id": "asset-old"}}))
    for snapshot, comment in [("asset-old", "历史列注释"), ("asset-new", "最新列注释")]:
        session.add(ColumnVersion(asset_version_id=snapshot, column_id="column", ordinal=1,
                                  name="segment", data_type="VARCHAR", comment=comment))
    dataset = session.scalar(select(AnalysisDataset).where(AnalysisDataset.run_id == run.id, AnalysisDataset.seq == 1))
    dataset.meta = {**dataset.meta, "versions": {"amount": "metric-old"}}
    session.commit()
    captured = {}

    def markdown(view, **kwargs):
        captured.update(view=view)
        return "export"

    monkeypatch.setattr(analysis.export, "to_markdown", markdown)
    response = client.get(f"/api/analysis/runs/{run.id}/export", params={"format": "md"})
    assert response.status_code == 200, response.text
    docs = captured["view"]["datasets"][0]["meta"]["export_labels"]["column_docs"]
    assert docs == ({"segment": {"comment": "历史列注释"}} if visible else {})


@pytest.mark.parametrize("format", ["md", "pdf"])
@pytest.mark.parametrize(("params", "audience"), [
    ({}, "brief"),
    ({"audience": "brief"}, "brief"),
    ({"audience": "full"}, "full"),
    ({"appendix": "false"}, "brief"),
    ({"appendix": "true"}, "full"),
    ({"audience": "brief", "appendix": "true"}, "brief"),
    ({"audience": "full", "appendix": "false"}, "full"),
])
def test_export_resolves_audience_and_legacy_appendix(api, monkeypatch, format, params, audience):
    client, _, run = api
    captured = {}

    def render(view, **kwargs):
        captured.update(kwargs)
        return "export" if format == "md" else b"%PDF-offline"

    monkeypatch.setattr(analysis.export, "to_markdown" if format == "md" else "to_pdf", render)
    response = client.get(f"/api/analysis/runs/{run.id}/export", params={"format": format, **params})
    assert response.status_code == 200, response.text
    assert captured == {"audience": audience, "datasets": None}


@pytest.mark.parametrize("audience", ["", "Brief", "FULL", "analyst", "brief,full"])
def test_export_rejects_invalid_audience(api, audience):
    client, _, run = api
    response = client.get(f"/api/analysis/runs/{run.id}/export", params={"format": "md", "audience": audience})
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("datasets", ["", "D0", "D01", "d1", "D-1", "D1,D3", "D99", "D1,", "D1,,D2", "D1 D2"])
def test_export_rejects_invalid_or_other_run_dataset_ids(api, datasets):
    client, _, run = api
    response = client.get(f"/api/analysis/runs/{run.id}/export", params={"format": "md", "datasets": datasets})
    assert response.status_code == 422, response.text


def test_export_markdown_selection_and_optional_appendix(api):
    client, _, run = api
    path = f"/api/analysis/runs/{run.id}/export"
    response = client.get(path, params={"format": "md", "datasets": "D2", "appendix": "false"})
    assert response.status_code == 200, response.text
    assert "| 甲 |" in response.text
    assert "类别 0" not in response.text
    assert "D2" not in response.text
    assert "## 附录" not in response.text
    assert "## 步骤" not in response.text
    response = client.get(path, params={"format": "md", "datasets": "D1", "audience": "full"})
    assert response.status_code == 200, response.text
    assert "## 附录" in response.text
    assert "共 60 行，显示前 10 行" in response.text
