"""续问历史证据在工具读取、计算和暂停恢复时按当前来源权限复核。"""

import json

import pytest
from sqlalchemy import JSON, MetaData, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from avelis.analysis.tools import AnalysisToolbox, Budget, PermissionDenied, PermissionLost
from avelis.ask.service import Catalog
from avelis.db import Base
from avelis.models import (
    AnalysisDataset, AnalysisRun, Asset, Datasource, Domain, DomainMember, User, UserRole,
)


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
