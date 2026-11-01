"""续问历史证据在工具读取、计算和暂停恢复时按当前来源权限复核。"""

import json
from unittest.mock import Mock

import pytest
from sqlalchemy import JSON, MetaData, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from avelis.analysis import service
from avelis.analysis.tools import AnalysisToolbox, Budget, PermissionDenied, PermissionLost
from avelis.ask import agent as ask_agent
from avelis.ask.service import Catalog
from avelis.db import Base
from avelis.models import (
    AnalysisDataset, AnalysisRun, Asset, Datasource, Domain, DomainMember, Job, User, UserRole,
)
from tests.unit.test_analysis_conversations import model
from tests.unit.test_analysis_dataset_api import api


@pytest.fixture
def copied_box():
    engine = create_engine("sqlite://")
    metadata = MetaData()
    for source in Base.metadata.tables.values():
        table = source.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        user = User(id="reader", username="reader", display_name="阅读者", password_hash="unused")
        asset = Asset(id="source-asset", datasource_id="source", kind="table", catalog_name="local",
                      db_name="data", name="facts", domain_id="visible")
        run = AnalysisRun(id="followup", user_id=user.id, question="继续分析", trigger="followup",
                          status="running", model_adapter="offline", window={})
        source = AnalysisDataset(id="copied", run_id=run.id, seq=1, kind="context", title="历史证据",
                                 columns=["value"], rows=[[17]], meta={"asset_ids": [asset.id],
                                     "units": {"value": {"kind": "count", "unit": "", "scale": 1}},
                                     "copied_from": {"run_id": "previous", "ref": "D4"}})
        calculated = AnalysisDataset(id="calculated", run_id=run.id, seq=2, kind="calc", title="本轮计算",
                                     inputs=["D1"], columns=["value"], rows=[[34]],
                                     meta={"closure": ["D1"], "summary": {"total": 34}})
        session.add_all([user, UserRole(user_id=user.id, role="analyst"),
                         Domain(id="visible", code="visible", name="可见域", owner_id=user.id),
                         Domain(id="hidden", code="hidden", name="其他域", owner_id=user.id),
                         DomainMember(domain_id="visible", user_id=user.id, role="viewer"),
                         Datasource(id="source", name="source", kind="fake", host="offline", port=1),
                         asset, run, source, calculated])
        session.commit()
        box = AnalysisToolbox(session, user, run, Catalog([], {}), Budget())
        box._datasets = {"D1": source, "D2": calculated}
        yield box, asset
    engine.dispose()


def test_copied_evidence_remains_available_with_units_and_original_provenance(copied_box):
    box, _ = copied_box
    view = json.loads(box.view_dataset("D1"))
    assert view["rows"] == [[17]] and view["units"]["value"]["kind"] == "count"
    assert box._rows("D2")[1] == [{"value": 34}]
    assert set(box.readable_datasets()) == {"D1", "D2"}
    restored = AnalysisToolbox(box.session, box.user, box.run, Catalog([], {}), Budget())
    restored.restore(box.snapshot())
    assert restored.datasets()["D1"].meta["copied_from"] == {"run_id": "previous", "ref": "D4"}


@pytest.mark.parametrize("ref", ["D1", "D2"])
def test_rows_and_verification_candidates_recheck_copied_source_closure(copied_box, ref):
    box, asset = copied_box
    asset.domain_id = "hidden"
    box.session.commit()
    with pytest.raises(PermissionDenied, match="你已没有该数据的访问权限"):
        box._rows(ref)
    assert box.readable_datasets() == {}
    assert box.denials[-1]["dataset"] == ref


def test_tool_call_stops_after_copied_source_is_revoked(copied_box):
    box, asset = copied_box
    assert json.loads(box.view_dataset("D1"))["rows"] == [[17]]
    asset.domain_id = "hidden"
    box.session.commit()
    result = json.loads(box.view_dataset("D1"))
    assert "error" in result and "rows" not in result
    assert box.permission_lost == "续问继承的数据集已没有访问权限，分析停止"


@pytest.mark.parametrize("phase", ["restore", "resume"])
def test_resume_stops_before_saved_model_context_can_reuse_revoked_copies(copied_box, phase):
    box, asset = copied_box
    snapshot = box.snapshot()
    asset.domain_id = "hidden"
    box.session.commit()
    restored = AnalysisToolbox(box.session, box.user, box.run, Catalog([], {}), Budget())
    with pytest.raises(PermissionLost, match="续问继承的数据集已没有访问权限"):
        if phase == "restore":
            restored.restore(snapshot)
        else:
            box.check_resume_permissions()


def test_current_run_without_copied_sources_keeps_existing_dataset_behavior(copied_box):
    box, asset = copied_box
    source = box.datasets()["D1"]
    source.meta = {key: value for key, value in source.meta.items() if key != "copied_from"}
    asset.domain_id = "hidden"
    box.session.commit()
    box.check_copied_permissions()
    assert set(box.readable_datasets()) == {"D1", "D2"}
    assert box._rows("D1")[1] == [{"value": 17}]


def test_omitted_copies_keep_their_dataset_numbers_reserved_across_resume(copied_box):
    box, asset = copied_box
    box._datasets = {}
    box._dataset_seq_floor = 2
    restored = AnalysisToolbox(box.session, box.user, box.run, Catalog([], {}), Budget())
    restored.restore(box.snapshot())
    dataset = restored._dataset("context", "本轮新证据", ["value"], [[29]],
                                meta={"asset_ids": [asset.id]})
    assert dataset.seq == 3 and set(restored.datasets()) == {"D3"}


@pytest.fixture
def waiting_followup(api, model):
    api.dataset.rows = [["copied-private-row", 17]]
    run = service.followup_run(api.session, api.user, api.run.id, "继续核对")
    derived = AnalysisDataset(run_id=run.id, seq=2, kind="calc", title="本轮计算", inputs=["D1"],
                              columns=["value"], rows=[["derived-private-row"]], meta={"closure": ["D1"]})
    api.session.add(derived)
    api.session.flush()
    box = AnalysisToolbox(api.session, api.user, run, Catalog([], {}), Budget())
    box._datasets = {f"D{dataset.seq}": dataset for dataset in api.session.scalars(
        select(AnalysisDataset).where(AnalysisDataset.run_id == run.id))}
    assert set(box.readable_datasets()) == {"D1", "D2"}
    run.status = "waiting"
    run.checkpoint = {**run.checkpoint, "snapshot": box.snapshot(),
                      "pending": {"kind": "clarify", "question": "继续核对哪个范围？"}}
    api.session.commit()
    membership = api.session.scalar(select(DomainMember).where(
        DomainMember.domain_id == "asset-domain", DomainMember.user_id == api.user.id))
    api.session.delete(membership)
    api.session.commit()
    return api, run


def test_waiting_stop_after_copied_access_revocation_allows_archive_and_delete(waiting_followup, monkeypatch):
    api, run = waiting_followup
    saver = Mock()
    monkeypatch.setattr(ask_agent, "checkpointer", lambda: saver)
    response = api.client.post(f"/api/analysis/runs/{run.id}/stop")
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["status"] == run.status == "stopped" and view["finished_at"]
    assert view["report"]["incomplete"] and view["report"]["permission"]["lost"]
    assert "pending" not in run.checkpoint and not run.checkpoint["resume_pending"]
    assert all(dataset["restricted"] and dataset["rows"] == [] for dataset in view["datasets"])
    assert "copied-private-row" not in response.text and "derived-private-row" not in response.text
    detail = api.client.get(f"/api/analysis/runs/{run.id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["report"] == view["report"]
    assert "copied-private-row" not in detail.text and "derived-private-row" not in detail.text
    path = f"/api/analysis/conversations/{api.run.id}"
    response = api.client.post(path + "/archive")
    assert response.status_code == 200, response.text
    response = api.client.delete(path)
    assert response.status_code == 204, response.text
    assert api.client.get(path).status_code == 404


def test_resumed_execution_still_rejects_revoked_copied_evidence(waiting_followup, monkeypatch):
    from avelis import jobs

    api, run = waiting_followup
    monkeypatch.setattr(jobs, "_wake", lambda job_id: None)
    monkeypatch.setattr(service, "warm_up", lambda: None)
    graph = Mock()
    monkeypatch.setattr(service, "_graph", graph)
    response = api.client.post(f"/api/analysis/runs/{run.id}/resume", json={"answer": "继续检查本次范围"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "queued"
    job = api.session.get(Job, run.budget["job_id"])
    job.status, job.attempts = "running", 1
    api.session.commit()
    outcome = service.execute(api.session, run.id, job.id, 1)
    assert outcome["status"] == run.status == job.status == "failed"
    assert "续问继承的数据集已没有访问权限" in run.error
    assert run.report["permission"]["lost"]
    assert run.finished_at is not None
    graph.assert_not_called()
