"""多指标维度对比及共享图表工具偏好契约的回归。"""

import json
from copy import deepcopy

import pytest

from avelis import charts
from avelis.analysis import dataset_access
from avelis.analysis.figures import dataset_charts, report_figures
from avelis.analysis.tools import ChartArgs as AnalysisChartArgs
from avelis.ask.agent import CHART_DESCRIPTION
from avelis.ask.agent import ChartArgs as AskChartArgs
from tests.unit.test_analysis_toolbox import make_box
from tests.unit.test_ask_chart_p2 import chart_reply, explain
from tests.unit.test_chart_ratio_delta_reachability import comparison_box


def comparison_dataset(count=2, *, mixed=False):
    columns = ["区域"]
    roles = {"区域": {"role": "dimension"}}
    units = {}
    for index in range(count):
        name = f"指标{index}"
        group = [name, f"{name}·对比期", f"{name}·变化", f"{name}·变化率"]
        columns.extend(group)
        roles.update({c: {"role": role} for c, role in zip(group, ("measure", "measure", "change", "rate"))})
        unit = f"单位{index}" if mixed else "元"
        units.update(
            {
                group[0]: {"kind": "level", "unit": unit, "additive": True},
                group[1]: {"kind": "level", "unit": unit, "additive": True},
                group[2]: {"kind": "delta", "unit": unit, "additive": True},
                group[3]: {"kind": "rate_change", "additive": False},
            }
        )
    rows = [
        [member, *[value for index in range(count) for value in (20 + index, 10 + index, 10, 1)]]
        for member in ("甲", "乙")
    ]
    return {
        "ref": "D1",
        "kind": "calc",
        "title": "指标对比",
        "columns": columns,
        "rows": rows,
        "column_roles": roles,
        "meta": {"column_roles": roles, "units": units},
    }


def test_same_unit_comparisons_keep_all_groups_in_every_candidate():
    data = comparison_dataset()
    before = deepcopy(data)
    default, candidate = dataset_charts(data)[:2]
    # 发散条形按涨跌着色，分不出多条系列属于哪个指标：多指标变化图每个指标一个面板。
    assert default["kind"] == "multiples"
    assert [s["column"] for s in default["series"]] == ["指标0·变化", "指标1·变化"]
    assert [s["axis"] for s in default["series"]] == [0, 1]
    assert candidate["kind"] == "grouped_bar"
    assert [s["column"] for s in candidate["series"]] == ["指标0", "指标0·对比期", "指标1", "指标1·对比期"]
    assert [s["name"] for s in candidate["series"]] == [
        "指标0 / 本期",
        "指标0 / 对比期",
        "指标1 / 本期",
        "指标1 / 对比期",
    ]
    assert all(s["axis"] == 0 for s in candidate["series"])
    assert all(chart["dropped_series"] == 0 for chart in (default, candidate))
    assert data == before


def test_single_metric_comparison_keeps_the_diverging_change_chart():
    default, candidate = dataset_charts(comparison_dataset(1))[:2]
    assert default["kind"] == "diverging_bar" and [s["axis"] for s in default["series"]] == [0]
    assert candidate["kind"] == "grouped_bar"


@pytest.mark.parametrize("count", [9])
def test_same_unit_comparison_panels_record_omission_over_the_limit(count):
    default = dataset_charts(comparison_dataset(count))[0]
    assert default["kind"] == "multiples" and len(default["series"]) == charts.MAX_PANELS
    assert default["dropped_series"] == count - charts.MAX_PANELS


@pytest.mark.parametrize("count", [2, 10])
def test_mixed_unit_comparison_panels_cover_every_group_or_record_omission(count):
    options = dataset_charts(comparison_dataset(count, mixed=True))
    assert options[0]["kind"] == "multiples"
    legacy_options = [chart for chart in options if chart["kind"] in {"multiples", "grouped_bar"}]
    assert len(legacy_options) == 1
    for chart in legacy_options:
        assert chart["kind"] == "multiples" and chart["reason"] == "change"
        assert [s["column"] for s in chart["series"]] == [f"指标{i}·变化" for i in range(min(count, charts.MAX_PANELS))]
        assert [s["axis"] for s in chart["series"]] == list(range(min(count, charts.MAX_PANELS)))
        assert chart["dropped_series"] == max(0, count - charts.MAX_PANELS)


def test_comparison_panel_identity_keeps_ratio_deltas_separate():
    data = comparison_dataset()
    data["meta"]["units"]["指标1·变化"]["of_ratio"] = True
    assert dataset_charts(data)[0]["kind"] == "multiples"


@pytest.mark.parametrize("mixed", [False, True])
def test_measures_single_selection_preserves_existing_single_comparison(mixed):
    data = comparison_dataset(mixed=mixed)
    selected = dataset_charts(data, {"kind": "grouped_bar", "measures": ["指标1"]})
    assert selected[0]["kind"] == "grouped_bar"
    assert [s["column"] for s in selected[0]["series"]] == ["指标1", "指标1·对比期"]
    assert [s["name"] for s in selected[0]["series"]] == ["本期", "对比期"]
    single = comparison_dataset(1)
    assert [s["name"] for s in dataset_charts(single)[1]["series"]] == ["本期", "对比期"]
    assert dataset_charts(single, {"kind": "diverging_bar", "measures": ["指标0"]}) == dataset_charts(single)


@pytest.mark.parametrize("mixed,kind", [(False, "grouped_bar"), (True, "multiples")])
def test_measures_order_applies_to_all_candidates_and_saved_figures(mixed, kind):
    data = comparison_dataset(mixed=mixed)
    preference = {"kind": kind, "title": "对比结论", "measures": ["指标1", "指标0"]}
    options = dataset_charts(data, preference)
    for chart in (chart for chart in options if chart["kind"] in {"multiples", "grouped_bar"}):
        assert chart["series"][0]["column"].startswith("指标1")
        assert chart["series"][-1]["column"].startswith("指标0")
    view = {"datasets": [data], "report": {"figures": [{"dataset": "D1", **preference}]}}
    assert report_figures(view)[0]["chart"] == options[0]


def test_comparison_metric_name_accepts_sql_alias():
    data = comparison_dataset()
    data["columns"][1] = "sql_alias"
    data["column_roles"]["sql_alias"] = data["column_roles"].pop("指标0")
    data["meta"]["units"]["sql_alias"] = data["meta"]["units"].pop("指标0")
    for requested in ("指标0", "sql_alias"):
        options = dataset_charts(data, {"kind": "grouped_bar", "measures": [requested]})
        assert [s["column"] for s in options[0]["series"]] == ["sql_alias", "指标0·对比期"]


def test_measures_also_select_and_order_plain_dataset_columns():
    data = {
        "columns": ["区域", "收入", "成本"],
        "rows": [["甲", 20, 10], ["乙", 30, 15]],
        "column_roles": {"区域": {"role": "dimension"}, "收入": {"role": "measure"}, "成本": {"role": "measure"}},
        "units": {column: {"kind": "level", "unit": "元", "additive": True} for column in ("收入", "成本")},
    }
    single = dataset_charts(data, {"kind": "bar", "measures": ["成本"]})[0]
    assert [s["column"] for s in single["series"]] == ["成本"]
    ordered = dataset_charts(data, {"kind": "grouped_bar", "measures": ["成本", "收入"]})[0]
    assert [s["column"] for s in ordered["series"]] == ["成本", "收入"]


@pytest.mark.parametrize("measures", [["不存在"], [], "指标0", [1]])
def test_invalid_measure_preference_is_ignored_as_a_whole(measures):
    data = comparison_dataset()
    assert dataset_charts(data, {"kind": "grouped_bar", "title": "应整体忽略", "measures": measures}) == dataset_charts(
        data
    )


def test_analysis_tool_rejects_unknown_measures_and_can_select_a_mixed_unit_group(monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    monkeypatch.setattr(dataset_access, "can_read_dataset", lambda *args: True)
    data = comparison_dataset(mixed=True)
    box._dataset("calc", data["title"], data["columns"], data["rows"], meta=data["meta"])
    result = json.loads(
        box.dispatch("chart", {"dataset": "D1", "kind": "grouped_bar", "title": "对比结论", "measures": ["不存在"]})
    )
    assert "不存在" in result["error"] and "可选 measures" in result["error"]
    assert "指标0" in result["error"] and "指标1" in result["error"]
    assert box.figures == []
    result = json.loads(
        box.dispatch("chart", {"dataset": "D1", "kind": "grouped_bar", "title": "对比结论", "measures": ["指标1"]})
    )
    assert result == {"ok": True}
    assert box.figures[0]["measures"] == ["指标1"]


def test_both_agents_derive_preference_schema_and_help_from_one_definition(monkeypatch):
    shared = charts.ChartPreference.model_json_schema()["properties"]
    assert AskChartArgs is charts.ChartPreference
    analysis = AnalysisChartArgs.model_json_schema()["properties"]
    assert {name: value for name, value in analysis.items() if name != "dataset"} == shared
    box = make_box(monkeypatch=monkeypatch)
    tool = next(t for t in box.tools() if t.name == "chart")
    assert charts.ChartPreference.tool_description() in tool.description
    assert charts.ChartPreference.tool_description() in CHART_DESCRIPTION


def test_ask_tool_accepts_kind_available_after_measure_selection(monkeypatch):
    _, answer = comparison_box(monkeypatch)
    model, _ = explain(monkeypatch, answer, chart_reply("grouped_bar", text="对比结果。", measures=["数量"]))
    assert answer.evidence["chart"]["kind"] == "grouped_bar"
    assert answer.evidence["chart"]["measures"] == ["数量"]
    assert model.bind_tools.call_args.args[0][0].args_schema is charts.ChartPreference


def test_legacy_saved_comparison_kind_keeps_new_mixed_unit_panels():
    data = comparison_dataset(mixed=True)
    view = {"datasets": [data], "report": {"figures": [{"dataset": "D1", "kind": "grouped_bar", "title": "对比结论"}]}}
    figure = report_figures(view)[0]
    assert figure["chart"]["kind"] == "multiples"
    assert len(figure["chart"]["series"]) == 2
    assert figure["title"] == "对比结论"


def test_measures_accept_the_display_label_the_model_sees_and_reject_ambiguous_labels():
    roles = {
        "区域": {"role": "dimension"},
        "amount": {"role": "measure", "label": "成交额"},
        "orders": {"role": "measure", "label": "订单数"},
        "x1": {"role": "measure", "label": "重复名"},
        "x2": {"role": "measure", "label": "重复名"},
    }
    columns = list(roles)
    assert charts.selected_measures(columns, roles, ["成交额"]) == ["amount"]
    assert charts.selected_measures(columns, roles, ["订单数", "amount"]) == ["orders", "amount"]
    with pytest.raises(ValueError, match="x1、x2"):
        charts.selected_measures(columns, roles, ["重复名"])
    assert charts.selected_measures(columns, roles, ["x2"]) == ["x2"]
