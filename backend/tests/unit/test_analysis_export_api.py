"""导出 HTTP 契约：真实路由、服务与内存 SQLite，不连接外部服务。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import JSON, MetaData, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from avelis.api import analysis
from avelis.api.deps import current_user, get_session
from avelis.models import AnalysisDataset, AnalysisMessage, AnalysisRun, AnalysisStep, AnalysisTask, User


@pytest.fixture
def api():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in (User, AnalysisTask, AnalysisRun, AnalysisStep, AnalysisDataset, AnalysisMessage):
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
    assert captured["kwargs"] == {"appendix": True, "datasets": None}
    dataset = captured["view"]["datasets"][0]
    assert len(dataset["rows"]) == dataset["row_count"] == 60
    assert dataset["rows"][-1] == ["类别 59", 59000]
    assert dataset["meta"]["units"]["指标"] == {"kind": "level", "unit": "元"}


def test_export_pdf_passes_requested_appendix_and_dataset_order(api, monkeypatch):
    client, _, run = api
    captured = {}

    def pdf(view, **kwargs):
        captured.update(view=view, kwargs=kwargs)
        return b"%PDF-offline"

    monkeypatch.setattr(analysis.export, "to_pdf", pdf)
    response = client.get(f"/api/analysis/runs/{run.id}/export", params={
        "appendix": "false", "datasets": "D2, D1,D2"})
    assert response.status_code == 200, response.text
    assert response.content == b"%PDF-offline"
    assert captured["kwargs"] == {"appendix": False, "datasets": ["D2", "D1"]}
    assert len(captured["view"]["datasets"][0]["rows"]) == 60


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
    assert "对照明细" in response.text
    assert "分组明细" not in response.text
    assert "## 附录" not in response.text
    assert "## 步骤" not in response.text
    response = client.get(path, params={"format": "md", "datasets": "D1"})
    assert response.status_code == 200, response.text
    assert "## 附录" in response.text
    assert "共 60 行，显示前 10 行" in response.text
