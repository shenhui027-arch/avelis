"""导出 HTTP 契约：真实路由、服务与内存 SQLite，不连接外部服务。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from reportlab.platypus import SimpleDocTemplate
from sqlalchemy import JSON, MetaData, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from avelis.api import analysis
from avelis.api.deps import current_user, get_session
from avelis.models import (
    AnalysisDataset,
    AnalysisMessage,
    AnalysisRun,
    AnalysisStep,
    AnalysisTask,
    Asset,
    AssetColumn,
    AssetVersion,
    CollectionRun,
    ColumnVersion,
    Datasource,
    Domain,
    DomainMember,
    IdentityLink,
    LineageEdge,
    Metric,
    MetricVersion,
    User,
    UserRole,
)
from tests.unit.test_analysis_export_audience import _pdf_text


@pytest.fixture
def api():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in (User, UserRole, Domain, DomainMember, Datasource, Asset, AssetColumn, CollectionRun,
                  AssetVersion, ColumnVersion, IdentityLink, LineageEdge, Metric, MetricVersion, AnalysisTask, AnalysisRun,
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
        session.add_all([
            user, run, other,
            Domain(id="domain", code="domain", name="可见域", owner_id=user.id),
            Domain(id="comments", code="comments", name="注释域", owner_id=user.id),
            DomainMember(domain_id="domain", user_id=user.id, role="viewer"),
            Datasource(id="source", name="source", kind="fake", host="offline", port=1),
            Asset(id="export-asset", datasource_id="source", kind="table", catalog_name="local", db_name="data",
                  name="facts", domain_id="domain"),
            Metric(id="export-metric", code="export_metric", name="基础指标", domain_id="domain",
                   owner_id=user.id, created_by=user.id),
            MetricVersion(id="export-version", metric_id="export-metric", version_no=1, status="published",
                          base_asset_id="export-asset", description="测试口径", expression="SUM(value)",
                          time_column="day", timezone="Asia/Shanghai", created_by=user.id),
        ])
        session.flush()
        session.add_all([
            AnalysisDataset(run_id=run.id, seq=1, kind="governed", title="分组明细", columns=["分组", "指标"],
                            rows=[[f"类别 {i}", i * 1000] for i in range(60)],
                            meta={"metric_version_ids": ["export-version"],
                                  "units": {"指标": {"kind": "level", "unit": "元"}},
                                  "intent": {"group_by": ["segment"]}}),
            AnalysisDataset(run_id=run.id, seq=2, kind="governed", title="对照明细", columns=["分组", "指标"],
                            rows=[["甲", 2000], ["乙", 1000]],
                            meta={"metric_version_ids": ["export-version"],
                                  "units": {"指标": {"kind": "level", "unit": "元"}}}),
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


def test_export_html_is_utf8_attachment_with_requested_dataset_order(api, monkeypatch):
    client, _, run = api
    captured = {}

    def html(view, **kwargs):
        captured.update(view=view, kwargs=kwargs)
        return "<!doctype html><title>分析报告</title>"

    monkeypatch.setattr(analysis.export, "to_html", html)
    response = client.get(f"/api/analysis/runs/{run.id}/export", params={
        "format": "html", "audience": "full", "datasets": "D2, D1,D2"})
    assert response.status_code == 200, response.text
    assert response.text == "<!doctype html><title>分析报告</title>"
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["content-disposition"] == f'attachment; filename="analysis-{run.id}.html"'
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
                                  base_asset_id="export-asset",
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
                      name="facts", domain_id="comments", current_version_id="asset-new"))
    if visible:
        session.add(DomainMember(domain_id="comments", user_id="actor", role="viewer"))
    session.add(MetricVersion(id="metric-old", metric_id="metric", version_no=1, status="published",
                              base_asset_id="export-asset",
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


@pytest.mark.parametrize("format", ["md", "pdf", "html"])
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
        return b"%PDF-offline" if format == "pdf" else "export"

    monkeypatch.setattr(analysis.export, {"md": "to_markdown", "pdf": "to_pdf", "html": "to_html"}[format], render)
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


@pytest.mark.parametrize("format", ["pdf", "md", "html"])
@pytest.mark.parametrize("audience", ["brief", "full"])
def test_export_does_not_restore_revoked_dataset_rows_sql_or_metadata(api, monkeypatch, format, audience):
    client, session, run = api
    user = session.get(User, "actor")
    restricted = session.scalar(select(AnalysisDataset).where(
        AnalysisDataset.run_id == run.id, AnalysisDataset.seq == 1))
    source = Asset(id="private-asset", datasource_id="source", kind="table", catalog_name="local", db_name="data",
                   name="private_facts", domain_id="domain")
    session.add_all([
        source,
        Domain(id="revoked", code="revoked", name="其他域", owner_id=user.id),
        Metric(id="private-metric", code="private_metric", name="受限指标", domain_id="domain",
               owner_id=user.id, created_by=user.id),
        MetricVersion(id="private-version", metric_id="private-metric", version_no=1, status="published",
                      base_asset_id=source.id, description="受限口径", expression="SUM(restricted_value)",
                      time_column="day", timezone="Asia/Shanghai", created_by=user.id),
    ])
    restricted.rows = [["受限成员甲", 7654321], ["受限成员乙", 8765432]]
    private_sql = "SELECT restricted_value FROM local.data.private_facts"
    restricted.meta = {**restricted.meta, "metric_version_ids": ["private-version"], "sql": private_sql,
                       "private_marker": "restricted-metadata-marker"}
    session.add(AnalysisStep(run_id=run.id, seq=1, kind="tool", tool="query_metric", status="ok",
                             dataset_id=restricted.id, args={"sql": private_sql},
                             output={"rows": restricted.rows, "sql": private_sql}))
    session.commit()
    assert analysis.analysis.run_view(session, run, user=user)["datasets"][0]["rows"] == restricted.rows

    # 来源改属不可读域，运行和历史原始行仍保留；HTTP 导出必须沿用当前权限投影。
    source.domain_id = "revoked"
    session.commit()
    projected = analysis.analysis.run_view(session, run, user=user)["datasets"][0]
    assert projected["restricted"] and projected["rows"] == [] and projected["sql"] is None
    metadata_calls, rendered_views, story = [], [], []
    metadata = analysis._export_metadata

    def enrich(session, user, meta):
        metadata_calls.append(meta)
        return metadata(session, user, meta)

    renderer_name = {"pdf": "to_pdf", "md": "to_markdown", "html": "to_html"}[format]
    renderer = getattr(analysis.export, renderer_name)

    def render(view, **kwargs):
        rendered_views.append(view)
        return renderer(view, **kwargs)

    monkeypatch.setattr(analysis, "_export_metadata", enrich)
    monkeypatch.setattr(analysis.export, renderer_name, render)
    if format == "pdf":
        monkeypatch.setattr(SimpleDocTemplate, "build", lambda self, flowables, *args, **kwargs: story.extend(flowables))
    response = client.get(f"/api/analysis/runs/{run.id}/export", params={
        "format": format, "audience": audience, "datasets": "D1,D2"})
    assert response.status_code == 200, response.text
    assert len(metadata_calls) == 1
    assert metadata_calls[0]["metric_version_ids"] == ["export-version"]
    hidden, readable = rendered_views[0]["datasets"]
    assert hidden["restricted"] and hidden["rows"] == [] and hidden["sql"] is None
    assert "meta" not in hidden
    assert readable["rows"] == [["甲", 2000], ["乙", 1000]]
    text = _pdf_text(story) if format == "pdf" else response.text
    for secret in ("受限成员甲", "受限成员乙", "7654321", "7,654,321", "8765432", "8,765,432",
                   private_sql, "restricted-metadata-marker"):
        assert secret not in text
    assert "甲" in text
    if audience == "full":
        assert "你已没有该数据的访问权限" in text
    else:
        assert "D1" not in text and "无访问权限" not in text
    session.refresh(restricted)
    assert restricted.rows == [["受限成员甲", 7654321], ["受限成员乙", 8765432]]
    assert restricted.meta["sql"] == private_sql


def test_export_csv_remains_available(api):
    client, _, run = api
    response = client.get(f"/api/analysis/runs/{run.id}/export", params={"format": "csv"})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == f'attachment; filename="analysis-{run.id}-sources.csv"'
