"""从真实问数计划装配和工具分发核实混合差额；只替换单指标查询的外部执行边界。"""

import json
from types import SimpleNamespace

import pytest

from avelis.analysis import export
from avelis.analysis.dataset_projection import dataset_meta
from avelis.analysis.figures import dataset_charts
from avelis.ask import service
from avelis.models import Metric, MetricVersion
from tests.unit.test_analysis_toolbox import make_box


def comparison_box(monkeypatch, *, temporal=False):
    pairs = {}
    objects = []
    for code, name, kind in [("conversion", "转化率", "ratio"), ("quantity", "数量", "count")]:
        metric = Metric(id=code, code=code, name=name, domain_id="dA", owner_id="u1", created_by="u1")
        version = MetricVersion(
            id=f"v-{code}",
            metric_id=code,
            version_no=1,
            kind="base",
            status="published",
            expression="SUM(b.value)",
            time_column="b.day",
            value_kind=kind,
            unit=None,
            unit_scale=1,
            dimensions=[{"name": "区域", "column": "b.region"}],
            data_grain="day",
            joins=[],
        )
        pairs[code] = (metric, version)
        objects += [metric, version]
    box = make_box(monkeypatch=monkeypatch, catalog=service.Catalog([], pairs), objects=objects)
    answers = []

    def start(session, user, question, adapter, cache):
        answer = SimpleNamespace(
            id="a1", question=question, model_adapter=adapter, metric_version_ids=[], evidence={}, freshness={}
        )
        answers.append(answer)
        return answer

    def query(session, user, intent, answer, versions, settings):
        # 只模拟数据库返回原始行；四列对比装配、量纲、角色和选图都走产品实现。
        current = intent.start_date.month == 9
        values = (
            ([0.2, 0.3] if current else [0.1, 0.2])
            if intent.metric_code == "conversion"
            else ([120, 140] if current else [100, 110])
        )
        answer.status = "answered"
        answer.result = {
            "rows": [
                ["2026-09" if temporal else region, value] for region, value in zip(["北", "南"], values, strict=True)
            ]
        }
        if temporal:
            answer.result["rows"][1][0] = "2026-10"
        answer.sql, answer.evidence, answer.freshness, answer.cache_hit = None, {}, {}, False

    monkeypatch.setattr(service, "start_answer", start)
    monkeypatch.setattr(service, "finish_answer", lambda *args: None)
    monkeypatch.setattr(service, "_fulfil", query)
    monkeypatch.setattr(service, "_version_evidence", lambda *args: {})
    monkeypatch.setattr(service, "validate_spec", lambda *args: {"aliases": {}})
    monkeypatch.setattr(service, "_dimension_docs", lambda *args: {})
    result = json.loads(
        box.dispatch(
            "query_metric",
            {
                "metric_codes": list(pairs),
                "start_date": "2026-09-01",
                "end_date": "2026-10-31",
                "compare": "custom",
                "compare_start": "2026-07-01",
                "compare_end": "2026-08-31",
                "group_by": [] if temporal else ["区域"],
                "time_grain": "month" if temporal else None,
                "purpose": "核实混合量纲差额的选图路径",
            },
        )
    )
    if temporal:
        return box, result
    assert "error" not in result, result
    return box, answers[0]


def test_time_grouped_comparison_is_rejected_before_dataset_creation(monkeypatch):
    box, result = comparison_box(monkeypatch, temporal=True)
    assert "按时间分组时不能同时" in result["error"]
    assert box.datasets() == {}


def test_real_comparison_and_analysis_tools_do_not_select_mixed_deltas(monkeypatch):
    box, answer = comparison_box(monkeypatch)
    ds = box.datasets()["D1"]
    units = dataset_meta(box.session, ds)["units"]
    assert units["转化率·变化"] == {"kind": "delta", "of_ratio": True, "additive": False}
    assert units["数量·变化"] == {"kind": "delta", "additive": True}
    assert len(ds.columns) == 9  # 分组键 + 每指标本期、对比期、差额、变化率。
    assert answer.result["column_roles"]["转化率·变化"]["role"] == "change"
    assert answer.result["column_roles"]["数量·变化"]["role"] == "change"
    data = {"columns": ds.columns, "rows": ds.rows, "meta": dataset_meta(box.session, ds)}
    charts = dataset_charts(data)
    selected = {s["column"] for chart in charts for s in chart["series"]}
    assert not {"转化率·变化", "数量·变化"} <= selected
    # 对比工具可以保留两种差额元数据，但同时产出 count 度量；选图继续优先 measure。
    segment = ds.columns[0]
    result = json.loads(
        box.dispatch(
            "segment_compare",
            {
                "dataset": "D1",
                "segment": segment,
                "measures": ["转化率·变化", "数量·变化"],
            },
        )
    )
    assert "error" not in result, result
    compared = box.datasets()["D2"]
    options = dataset_charts({"columns": compared.columns, "rows": compared.rows, "meta": compared.meta})
    assert options and all(s["column"] == "count" for chart in options for s in chart["series"])
    # 差额拆解要求可加性：比率被拒绝；数量可拆，但结果不含另一种差额。
    denied = json.loads(
        box.dispatch(
            "contribution",
            {
                "dataset": "D1",
                "keys": [segment],
                "current": "转化率",
                "previous": "转化率·对比期",
            },
        )
    )
    assert "不能按成员相加" in denied["error"]
    allowed = json.loads(
        box.dispatch(
            "contribution",
            {
                "dataset": "D1",
                "keys": [segment],
                "current": "数量",
                "previous": "数量·对比期",
            },
        )
    )
    assert "error" not in allowed, allowed
    assert box.datasets()["D3"].meta["units"]["change"] == {"kind": "delta"}


@pytest.mark.parametrize("temporal", [False, True])
def test_future_delta_only_projection_splits_of_ratio_panels(monkeypatch, temporal):
    box, _ = comparison_box(monkeypatch)
    ds = box.datasets()["D1"]
    # 防御性回归：当前工具不提供这一步投影；复用真实链产出的数据与 units，模拟将来的差额专用视图。
    columns = [ds.columns[0], "转化率·变化", "数量·变化"]
    data = {
        "columns": columns,
        "rows": [[row[ds.columns.index(c)] for c in columns] for row in ds.rows],
        "meta": {
            **ds.meta,
            "intent": {},
            "time_column": columns[0] if temporal else None,
            "units": {c: ds.meta["units"][c] for c in columns[1:]},
        },
    }
    specs = dataset_charts(data)
    assert specs[0]["kind"] == "multiples"
    assert [s["axis"] for s in specs[0]["series"]] == [0, 1]
    drawing = export._spec_chart(specs[0], export.pdf_font()[0])
    assert drawing.height > 0
    assert all(chart["kind"] == "multiples" or len(chart["series"]) == 1 for chart in specs)
