"""真实 SQLite 验证改图的历史结果复制、权限与完整规格读取。"""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest

from avelis.analysis import conversation_context as context
from avelis.analysis import service
from avelis.analysis.dataset_projection import RESTRICTED_TEXT
from avelis.analysis.tools import AnalysisToolbox
from avelis.api import analysis
from avelis.ask import service as ask_service
from avelis.governance.errors import Forbidden
from avelis.models import AnalysisDataset, Answer

from .test_analysis_conversation_context import child
from .test_analysis_dataset_access import dataset_db  # noqa: F401  pytest 夹具按参数名注入


def chart_data(source, *, count=3, additive=False):
    source.columns = ["category", "value"]
    source.rows = [[f"item{index}", index] for index in range(1, count + 1)]
    source.meta = {
        **source.meta,
        "intent": {"group_by": ["category"]},
        "units": {"value": {"kind": "level" if additive else "ratio", "additive": additive}},
        "column_labels": {"category": "成员", "value": "度量"},
    }


def lookup_answer(session, user, parent, source, version):
    chart_data(source, count=120)
    preference = {"kind": "bar", "title": "保存的图题", "sort": "category", "marks": ["max"]}
    result = {
        "columns": deepcopy(source.columns),
        "rows": deepcopy(source.rows),
        "truncated": True,
        "units": deepcopy(source.meta["units"]),
        "polarity": {"value": "higher_better"},
        "column_roles": {
            "category": {"role": "dimension", "label": "成员"},
            "value": {"role": "measure", "label": "度量", "additive": False},
        },
    }
    answer = Answer(
        id="lookup-answer",
        asked_by=user.id,
        question="查询成员度量",
        status="answered",
        model_adapter="offline",
        metric_version_ids=[version.id],
        intent={"group_by": ["category"]},
        sql="SELECT category, value FROM saved_source",
        result=result,
        evidence={"chart": preference},
    )
    parent.report = {"kind": "lookup", "answer_id": answer.id, "title": "保存的取数答案"}
    parent.conversation_id = parent.id
    session.delete(source)
    session.add(answer)
    session.flush()
    return answer, preference


def test_lookup_copies_all_saved_rows_without_querying_and_rebuild_is_idempotent(dataset_db, monkeypatch):
    session, user, parent, source, _, _, version = dataset_db
    answer, preference = lookup_answer(session, user, parent, source, version)
    original_result, original_evidence = deepcopy(answer.result), deepcopy(answer.evidence)
    run = child(session, parent)
    execute = Mock(side_effect=AssertionError("改图不得重新查询"))
    monkeypatch.setattr(ask_service, "fulfil", execute)
    monkeypatch.setattr(ask_service, "query_connector", execute)
    monkeypatch.setattr(AnalysisToolbox, "_run_query", execute)
    monkeypatch.setattr(AnalysisToolbox, "query_sql", execute)

    first = context.build_followup_context(session, user, parent, run)
    copied = context._datasets(session, run.id)
    assert set(copied) == {"D1"}
    dataset = copied["D1"]
    assert dataset.kind == "governed" and dataset.answer_id == answer.id
    assert dataset.columns == answer.result["columns"] and dataset.rows == answer.result["rows"]
    assert len(dataset.rows) == 120 and dataset.truncated
    assert dataset.meta["copied_from"] == {"run_id": parent.id, "ref": "answer"}
    assert dataset.meta["units"] == answer.result["units"]
    assert dataset.meta["column_roles"] == answer.result["column_roles"]
    assert dataset.meta["column_labels"] == {"category": "成员", "value": "度量"}
    assert dataset.meta["polarity"] == answer.result["polarity"] and dataset.meta["sql"] == answer.sql
    assert first["chart_targets"] == [
        {"dataset": "D1", "title": "保存的取数答案", "preference": preference, "current_kind": "bar"}
    ]

    again = context.build_followup_context(session, user, parent, run)
    refreshed = context.refresh_followup_context(session, user, run)
    assert context._datasets(session, run.id)["D1"].id == dataset.id
    assert len(context._datasets(session, run.id)) == 1
    assert again["chart_targets"] == refreshed["chart_targets"] == first["chart_targets"]
    execute.assert_not_called()
    assert answer.result == original_result and answer.evidence == original_evidence
    dataset.rows[0][1] = 999
    dataset.meta["units"]["value"]["kind"] = "count"
    assert answer.result == original_result


def test_report_targets_follow_figure_order_and_remap_to_current_dataset_refs(dataset_db):
    session, user, parent, source, *_ = dataset_db
    chart_data(source, additive=True)
    second = AnalysisDataset(
        id="second-dataset",
        run_id=parent.id,
        seq=2,
        kind="governed",
        title="第二份数据",
        columns=deepcopy(source.columns),
        rows=deepcopy(source.rows),
        meta=deepcopy(source.meta),
    )
    selected = [
        {"dataset": "D2", "kind": "donut", "title": "成员占比", "highlight": ["item2"]},
        {"dataset": "D1", "kind": "bar", "title": "成员排名", "sort": "value_asc", "marks": ["max"]},
    ]
    parent.report = {"title": "双图报告", "figures": selected}
    session.add(second)
    run = child(session, parent)
    session.add(
        AnalysisDataset(run_id=run.id, seq=1, kind="context", title="本轮既有证据", columns=[], rows=[], meta={})
    )
    session.flush()
    original = deepcopy(parent.report), deepcopy(source.rows), deepcopy(source.meta)

    history = context.build_followup_context(session, user, parent, run)
    assert history["chart_targets"] == [
        {
            "dataset": "D3",
            "title": "成员占比",
            "preference": {key: value for key, value in selected[0].items() if key != "dataset"},
            "current_kind": "donut",
        },
        {
            "dataset": "D2",
            "title": "成员排名",
            "preference": {key: value for key, value in selected[1].items() if key != "dataset"},
            "current_kind": "bar",
        },
    ]
    assert (parent.report, source.rows, source.meta) == original


@pytest.mark.parametrize("lookup", [False, True])
def test_revocation_hides_targets_and_saved_chart_preference_and_rejects_dataset_reads(dataset_db, lookup):
    session, user, parent, source, asset, _, version = dataset_db
    if lookup:
        lookup_answer(session, user, parent, source, version)
    else:
        chart_data(source)
        parent.report = {"figures": [{"dataset": "D1", "kind": "bar", "title": "成员图"}]}
    run = child(session, parent)
    assert context.build_followup_context(session, user, parent, run)["chart_targets"]
    dataset = context._datasets(session, run.id)["D1"]
    preference = {"kind": "bar", "title": "item120 的图", "highlight": ["item120"]}
    run.report = {
        "kind": "chart",
        "dataset": "D1",
        "preference": preference,
        "title": preference["title"],
        "summary": "已调整成员图。",
        "figures": [{"dataset": "D1", **preference}],
    }
    original = deepcopy(run.report)
    asset.domain_id = "hidden"
    session.flush()

    history = context.refresh_followup_context(session, user, run)
    assert history["chart_targets"] == [] and history["copied_datasets"] == []
    view = service.run_view(session, run, user=user)
    assert view["report"]["restricted"] and view["report"]["title"] == RESTRICTED_TEXT
    assert service.run_view(session, run, full=False, user=user)["title"] == RESTRICTED_TEXT
    assert "preference" not in view["report"] and view["report"]["figures"] == []
    assert view["datasets"][0]["restricted"] and view["datasets"][0]["charts"] == []
    assert view["figures"] == [] and "item120" not in str(view["report"])
    with pytest.raises(Forbidden, match="你已没有该数据的访问权限"):
        analysis.get_dataset(run.id, "D1", offset=0, limit=1, user=user, session=session)
    assert run.report == original and len(dataset.rows) == (120 if lookup else 3)


def test_chart_run_details_and_pages_materialize_same_full_data_spec_without_mutating_source(dataset_db):
    session, user, parent, source, *_ = dataset_db
    chart_data(source, count=120)
    parent.report = {"figures": [{"dataset": "D1", "kind": "bar", "title": "上一轮图"}]}
    run = child(session, parent)
    context.build_followup_context(session, user, parent, run)
    original = deepcopy(source.rows), deepcopy(source.meta), deepcopy(parent.report)
    copied = context._datasets(session, run.id)["D1"]
    original_copied = deepcopy(copied.rows), deepcopy(copied.meta)
    preference = {
        "kind": "bar",
        "title": "已调整图表",
        "top_n": 2,
        "sort": "value_desc",
        "marks": ["max", "min", "average"],
        "highlight": ["item120"],
        "measures": ["value"],
    }
    run.report = {
        "kind": "chart",
        "dataset": "D1",
        "preference": preference,
        "title": "已调整图表",
        "summary": "已按要求调整图表。",
        "figures": [{"dataset": "D1", **preference}],
    }
    session.flush()

    view = service.run_view(session, run, user=user)
    first = analysis.get_dataset(run.id, "D1", offset=0, limit=1, user=user, session=session)
    last = analysis.get_dataset(run.id, "D1", offset=119, limit=1, user=user, session=session)
    assert len(view["datasets"][0]["rows"]) == 50 and view["datasets"][0]["row_count"] == 120
    assert first["rows"] == [["item1", 1]] and last["rows"] == [["item120", 120]]
    assert first["total"] == last["total"] == 120
    spec = view["datasets"][0]["charts"][0]
    assert spec == first["charts"][0] == last["charts"][0] == view["figures"][0]["chart"]
    assert spec["title"] == preference["title"] and spec["categories"] == ["item120", "item119"]
    assert spec["highlight"] == ["item120"]
    assert spec["marks"] == [
        {"type": "max", "series": "value", "series_index": 0, "x": "item120", "value": 120},
        {"type": "min", "series": "value", "series_index": 0, "x": "item119", "value": 119},
        {"type": "average", "series": "value", "series_index": 0, "x": None, "value": 119.5},
    ]
    assert (source.rows, source.meta, parent.report) == original
    assert (copied.rows, copied.meta) == original_copied


def test_chart_target_preference_does_not_overflow_followup_context_budget(dataset_db):
    session, user, parent, source, *_ = dataset_db
    chart_data(source)
    member = '"' * 13_000
    source.rows[0][0] = member
    session.add(
        AnalysisDataset(
            run_id=parent.id,
            seq=2,
            kind="governed",
            title="另一张图",
            columns=deepcopy(source.columns),
            rows=[["another", 2], ["third", 3]],
            meta=deepcopy(source.meta),
        )
    )
    parent.report = {
        "title": "成员图",
        "figures": [
            {"dataset": "D1", "kind": "bar", "title": "成员图", "highlight": [member]},
            {"dataset": "D2", "kind": "bar", "title": "另一张图"},
        ],
    }
    original = deepcopy(parent.report), deepcopy(source.rows)
    run = child(session, parent)

    history = context.build_followup_context(session, user, parent, run)

    assert len(json.dumps(history, ensure_ascii=False)) <= context.CONTEXT_CHARS
    assert len(history["chart_targets"]) == 2  # 不能靠删图缩减摘要，否则“多图需澄清”会误变成唯一目标。
    assert history["chart_targets"][0]["dataset"] == "D1"
    assert history["chart_targets"][0]["preference"] == {}
    assert history["chart_targets"][0]["preference_omitted"] is True
    assert (parent.report, source.rows) == original
