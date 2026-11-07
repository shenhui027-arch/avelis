"""六种新图的纯内存选图、偏好与导出契约。"""

import json
from copy import deepcopy
from xml.etree import ElementTree

import pytest
from pydantic import ValidationError
from reportlab.graphics import renderSVG

from avelis.analysis import export, figures
from avelis.api.schemas import ChartSpec
from avelis.charts import ChartPreference, recommend
from avelis.i18n import locale_context


def chart_data(columns, rows, *, dimensions=(), times=(), measures=("a",), units=None):
    roles = {column: {"role": "dimension", "label": "成员"} for column in dimensions}
    roles.update({column: {"role": "time", "label": "期间", "grain": "month"} for column in times})
    roles.update({column: {"role": "measure", "label": f"指标 {column}"} for column in measures})
    return {
        "columns": columns,
        "rows": rows,
        "roles": roles,
        "units": units or {column: {"kind": "level", "unit": "单位", "additive": True} for column in measures},
        "polarity": {},
    }


def temporal_data(count=1):
    measures = list("abcd")[:count]
    return chart_data(
        ["t", *measures],
        [[period, *(index + offset for offset in range(count))] for index, period in enumerate(("03", "01", "02"), 1)],
        times=["t"],
        measures=measures,
    )


def categorical_data(count=1, rows=None):
    measures = list("abcd")[:count]
    return chart_data(
        ["d", *measures],
        rows
        if rows is not None
        else [[f"m{index}", *(index + offset for offset in range(count))] for index in range(1, 5)],
        dimensions=["d"],
        measures=measures,
    )


def comparison_data(rows=None):
    columns = ["d", "a", "a·上一期", "a·变化", "a·变化率"]
    data = chart_data(
        columns,
        rows if rows is not None else [["甲", 120, 100, 20, 0.2], ["乙", 30, 80, -50, -0.625], ["丙", 60, 50, 10, 0.2]],
        dimensions=["d"],
        measures=columns[1:],
    )
    data["roles"]["a·变化"]["role"] = "change"
    data["roles"]["a·变化率"]["role"] = "rate"
    data["units"]["a·变化"].update(kind="delta")
    data["units"]["a·变化率"] = {"kind": "rate_change", "additive": False}
    return data


def heatmap_data():
    return chart_data(
        ["t", "d", "a"],
        [["03", "甲", 5], ["01", "甲", 1], ["01", "乙", 2], ["02", "甲", 3], ["02", "乙", 4]],
        times=["t"],
        dimensions=["d"],
    )


def kinds(data):
    before = deepcopy(data)
    charts = recommend(**data)
    assert data == before
    for chart in charts:
        json.dumps(chart, allow_nan=False)
    return [chart["kind"] for chart in charts]


def select(data, kind, **options):
    return ChartPreference(kind=kind, title="结果观察", **options).materialize(**data)


@pytest.mark.parametrize("count", [1, 2, 3])
def test_area_follows_existing_time_candidates_without_changing_default(count):
    data = temporal_data(count)
    options = recommend(**data)
    assert options[0]["kind"] == "line"
    assert [chart["kind"] for chart in options[:2]] == ["line", "bar" if count == 1 else "grouped_bar"]
    line = options[0]
    area = select(data, "area")
    assert area["categories"] == line["categories"] == ["01", "02", "03"]
    assert area["series"] == line["series"]
    assert all(series["mark"] == "line" for series in area["series"])


@pytest.mark.parametrize(
    "data", [temporal_data(4), categorical_data(), chart_data(["t", "a"], [["01", 1]], times=["t"])]
)
def test_area_requires_a_time_line_with_at_most_three_series(data):
    assert "area" not in kinds(data)


@pytest.mark.parametrize("temporal", [False, True])
@pytest.mark.parametrize("difference", ["unit", "ratio"])
def test_combo_is_an_additional_two_measure_dual_axis_candidate(temporal, difference):
    data = temporal_data(2) if temporal else categorical_data(2)
    data["units"]["b"].update({"unit": "另一单位"} if difference == "unit" else {"kind": "ratio"})
    options = kinds(data)
    assert options[0] == "multiples"
    assert options.index("combo") > options.index("line" if temporal else "bar")
    combo = select(data, "combo", measures=["b", "a"])
    assert [(series["column"], series["mark"], series["axis"]) for series in combo["series"]] == [
        ("b", "bar", 0),
        ("a", "line", 1),
    ]


def test_combo_accepts_two_ratio_measures_even_with_matching_units():
    data = temporal_data(2)
    for unit in data["units"].values():
        unit.update(kind="ratio", additive=False)
    assert kinds(data)[0] == "line"
    assert "combo" in kinds(data)


def test_combo_with_time_and_dimension_keeps_a_single_member_without_aggregating_members():
    data = chart_data(
        ["t", "d", "a", "b"],
        [[period, "甲", index, index / 10] for index, period in enumerate(("01", "02", "03"), 1)],
        times=["t"],
        dimensions=["d"],
        measures=["a", "b"],
    )
    data["units"]["b"].update(kind="ratio", additive=False)
    assert kinds(data)[0] == "multiples"
    assert select(data, "combo")["series"][1]["values"] == [0.1, 0.2, 0.3]
    data["rows"].extend([[period, "乙", 10, 0.9] for period in ("01", "02", "03")])
    assert "combo" not in kinds(data)


@pytest.mark.parametrize("count", [1, 2, 3])
def test_combo_requires_exactly_two_selected_measures_and_different_units(count):
    data = categorical_data(count)
    if count != 2:
        data["units"]["a"]["unit"] = "另一单位"
    assert "combo" not in kinds(data)
    if count == 3:
        assert len(select(data, "combo", measures=["a", "b"])["series"]) == 2


def test_pareto_sorts_descending_and_aligns_cumulative_with_top_n_other():
    data = categorical_data(rows=[[f"m{index}", index] for index in range(1, 7)])
    data["intent"] = {"order": "asc"}
    assert kinds(data)[:2] == ["bar", "donut"]
    with locale_context("en"):
        chart = select(data, "pareto", top_n=2, highlight=["Other"])
    assert chart["categories"] == ["m6", "m5", "Other"]
    assert chart["series"][0]["values"] == [6.0, 5.0, 10.0]
    assert chart["cumulative"] == pytest.approx([6 / 21, 11 / 21, 1])
    assert chart["others"] == {"count": 4}
    assert chart["highlight"] == ["Other"]


def test_pareto_zero_total_has_finite_zero_cumulative():
    chart = select(categorical_data(rows=[[f"m{i}", 0] for i in range(4)]), "pareto")
    assert chart["cumulative"] == [0.0] * 4
    json.dumps(chart, allow_nan=False)


def test_pareto_large_finite_values_keep_correct_cumulative_when_total_overflows():
    chart = select(categorical_data(rows=[[f"m{i}", 1e308] for i in range(4)]), "pareto")
    assert chart["series"][0]["values"] == [1e308] * 4
    assert chart["cumulative"] == [0.25, 0.5, 0.75, 1.0]
    json.dumps(chart, allow_nan=False)


@pytest.mark.parametrize("invalid", ["few", "negative", "missing", "nonadditive", "multiple", "time"])
def test_pareto_rejects_unsuitable_data(invalid):
    data = categorical_data()
    if invalid == "few":
        data["rows"] = data["rows"][:3]
    elif invalid == "negative":
        data["rows"][0][1] = -1
    elif invalid == "missing":
        data["rows"][0][1] = None
    elif invalid == "nonadditive":
        data["units"]["a"]["additive"] = False
    elif invalid == "multiple":
        data = categorical_data(2)
    else:
        data = temporal_data()
    assert "pareto" not in kinds(data)


def test_waterfall_uses_absolute_change_and_total_period_endpoints():
    data = comparison_data()
    assert kinds(data)[:2] == ["diverging_bar", "grouped_bar"]
    chart = select(data, "waterfall")
    assert chart["categories"] == ["乙", "甲", "丙"]
    assert chart["series"][0]["column"] == "a·变化"
    assert chart["series"][0]["values"] == [-50.0, 20.0, 10.0]
    assert chart["waterfall"]["start"]["value"] == 230.0
    assert chart["waterfall"]["end"]["value"] == 210.0
    assert chart["waterfall"]["start"]["label"]
    assert chart["waterfall"]["end"]["label"]


@pytest.mark.parametrize("change", [None, 0])
def test_comparison_omits_diverging_bar_only_when_all_displayed_changes_are_null(change):
    data = comparison_data([["A", 10, None, None, None], ["B", 4, None if change is None else 4, change, change]])
    for preference in (None, {"kind": "diverging_bar"}):
        options = recommend(**data, preference=preference)
        if change is None:
            assert [chart["kind"] for chart in options] == ["grouped_bar"]
            assert [series["column"] for series in options[0]["series"]] == ["a", "a·上一期"]
            assert [series["values"] for series in options[0]["series"]] == [[10.0, 4.0], [None, None]]
        else:
            assert [chart["kind"] for chart in options] == ["diverging_bar", "grouped_bar"]
            assert options[0]["series"][0]["values"] == [0.0, None]


def test_comparison_omits_diverging_bar_when_category_top_n_hides_all_present_changes():
    data = comparison_data([["A", 10, None, None, None], ["B", 4, None, None, None], ["C", 2, 1, 1, 1]])
    for unit in data["units"].values():
        unit["additive"] = False
    options = recommend(**data, preference={"kind": "diverging_bar", "top_n": 2, "sort": "category"})
    assert [chart["kind"] for chart in options] == ["grouped_bar"]
    assert options[0]["categories"] == ["A", "B"]
    assert [series["column"] for series in options[0]["series"]] == ["a", "a·上一期"]


def test_unavailable_non_change_chart_ignores_measure_preference_as_a_whole():
    data = categorical_data(count=2)
    assert recommend(**data, preference={"kind": "line", "measures": ["a"]}) == recommend(**data)


def test_waterfall_merges_tail_at_display_limit_and_keeps_full_totals():
    data = comparison_data([[f"m{i}", 100 + i, 100, i, i / 100] for i in range(22)])
    with locale_context("en"):
        chart = select(data, "waterfall")
    assert chart["categories"] == [*(f"m{i}" for i in range(21, 1, -1)), "Other"]
    assert chart["series"][0]["values"][-1] == 1.0
    assert chart["others"] == {"count": 2}
    assert chart["waterfall"]["start"]["value"] == 2200.0
    assert chart["waterfall"]["end"]["value"] == 2431.0


@pytest.mark.parametrize(
    "rows",
    [
        [["A", 10, None, None, None], ["B", 4, 1, 3, 3]],
        [["A", None, 5, None, None], ["B", 4, 1, 3, 3]],
        [["A", 10, 5, None, None], ["B", 4, 1, 3, 3]],
        [["A", 10, 5, 2, 0.4], ["B", 4, 1, 3, 3]],
        [["A", 1e308, 1e308, 0, 0], ["B", 1e308, 1e308, 0, 0]],
        [["A", 1e308, 0, 1e308, None], ["B", 1e308, 0, 1e308, None]],
    ],
)
def test_waterfall_omits_incomplete_inconsistent_or_overflowing_comparisons(rows):
    assert "waterfall" not in kinds(comparison_data(rows))


def test_waterfall_accepts_float_roundoff_without_changing_displayed_delta():
    data = comparison_data([["A", 0.3, 0.1, 0.2, 2], ["B", 0.4, 0.1, 0.3, 3]])
    chart = select(data, "waterfall")
    assert chart["series"][0]["values"] == [0.3, 0.2]
    assert chart["waterfall"]["start"]["value"] == 0.2
    assert chart["waterfall"]["end"]["value"] == 0.7


def test_comparison_multiple_measures_offer_new_candidates_after_existing_default():
    data = comparison_data()
    columns = ["b", "b·上一期", "b·变化", "b·变化率"]
    data["columns"].extend(columns)
    for index, row in enumerate(data["rows"], 1):
        row.extend([index / 10, 0.1, (index - 1) / 10, index - 1])
    for column, role in zip(columns, ["measure", "measure", "change", "rate"], strict=True):
        data["roles"][column] = {"role": role, "label": column}
        data["units"][column] = {"kind": "ratio", "additive": False}
    assert kinds(data) == ["multiples", "combo", "waterfall", "scatter"]
    assert [series["column"] for series in select(data, "combo")["series"]] == ["a", "b"]
    assert [series["column"] for series in select(data, "scatter")["series"]] == ["a", "b"]
    assert select(data, "waterfall")["series"][0]["column"] == "a·变化"


@pytest.mark.parametrize("invalid", ["single", "dimension", "comparison", "current", "previous", "change"])
def test_waterfall_requires_dimension_comparison_and_additive_periods(invalid):
    data = comparison_data()
    if invalid == "single":
        data["rows"] = data["rows"][:1]
    elif invalid == "dimension":
        data["roles"]["d"]["role"] = "time"
    elif invalid == "comparison":
        data["roles"]["a·变化"]["role"] = "measure"
    else:
        column = {"current": "a", "previous": "a·上一期", "change": "a·变化"}[invalid]
        data["units"][column]["additive"] = False
    assert "waterfall" not in kinds(data)


def test_heatmap_preserves_sorted_time_full_grid_missing_cells_and_unit_series():
    data = heatmap_data()
    assert kinds(data)[:2] == ["line", "stacked_bar"]
    chart = select(data, "heatmap")
    assert chart["categories"] == ["01", "02", "03"]
    assert chart["heatmap"] == {
        "y": {"column": "d", "label": "成员"},
        "y_categories": ["甲", "乙"],
        "cells": [[0, 0, 1.0], [0, 1, 2.0], [1, 0, 3.0], [1, 1, 4.0], [2, 0, 5.0], [2, 1, None]],
        "min": 1.0,
        "max": 5.0,
    }
    assert len(chart["series"]) == 1
    assert chart["series"][0]["column"] == "a"
    assert chart["series"][0]["values"] == []
    assert chart["series"][0]["unit"] == data["units"]["a"]


def test_heatmap_selects_top_twelve_members_by_sum_without_merging():
    data = heatmap_data()
    data["rows"] = [[period, f"m{i}", i] for period in ("01", "02", "03") for i in range(14)]
    chart = select(data, "heatmap")
    assert chart["heatmap"]["y_categories"] == [f"m{i}" for i in range(13, 1, -1)]
    assert len(chart["heatmap"]["cells"]) == 36
    assert chart["heatmap"]["min"] == 2.0 and chart["heatmap"]["max"] == 13.0
    assert chart["dropped_series"] == 2
    assert chart["others"] is None


@pytest.mark.parametrize("empty_time", [None, "", " "])
def test_heatmap_does_not_count_missing_time_as_a_period(empty_time):
    data = heatmap_data()
    data["rows"] = [[period, member, 1] for period in (empty_time, "01", "02") for member in ("甲", "乙")]
    options = recommend(**data)
    assert options[0]["kind"] == "line"
    assert empty_time in options[0]["categories"]
    assert "heatmap" not in [chart["kind"] for chart in options]


def test_heatmap_filters_missing_time_and_members_seen_only_in_missing_periods():
    data = heatmap_data()
    data["rows"].extend([[None, "甲", 100], [None, "丙", 200], ["", "乙", 100]])
    chart = select(data, "heatmap")
    assert chart["categories"] == ["01", "02", "03"]
    assert chart["heatmap"]["y_categories"] == ["甲", "乙"]
    assert chart["heatmap"]["max"] == 5.0


@pytest.mark.parametrize("invalid", ["periods", "members", "dimension", "time", "measure", "nulls"])
def test_heatmap_requires_one_time_one_dimension_one_measure_and_sufficient_grid(invalid):
    data = heatmap_data()
    if invalid == "periods":
        data["rows"] = [row for row in data["rows"] if row[0] != "03"]
    elif invalid == "members":
        data["rows"] = [row for row in data["rows"] if row[1] == "甲"]
    elif invalid in ("dimension", "time"):
        column = "extra"
        data["columns"].insert(0, column)
        for row in data["rows"]:
            row.insert(0, "fixed")
        data["roles"][column] = {"role": invalid, "label": column}
    elif invalid == "measure":
        data["columns"].append("b")
        data["roles"]["b"] = {"role": "measure", "label": "指标 b"}
        data["units"]["b"] = data["units"]["a"].copy()
        for row in data["rows"]:
            row.append(1)
    else:
        for row in data["rows"]:
            row[-1] = None
    assert "heatmap" not in kinds(data)


def test_scatter_uses_selected_measure_order_for_x_y_and_optional_size():
    data = categorical_data(3, rows=[["甲", 1, 9, 10], ["乙", 3, 1, None], ["丙", 2, 4, 30]])
    assert kinds(data)[0] == "grouped_bar"
    chart = select(data, "scatter", measures=["b", "a", "c"])
    assert chart["categories"] == ["甲", "乙", "丙"]
    assert [series["column"] for series in chart["series"]] == ["b", "a", "c"]
    assert chart["points"] == [
        {"name": "甲", "x": 9.0, "y": 1.0, "size": 10.0},
        {"name": "乙", "x": 1.0, "y": 3.0, "size": None},
        {"name": "丙", "x": 4.0, "y": 2.0, "size": 30.0},
    ]
    assert all(len(series["values"]) == len(chart["categories"]) for series in chart["series"])
    assert all(point["size"] is None for point in select(data, "scatter", measures=["a", "b"])["points"])


@pytest.mark.parametrize("invalid", ["measures", "rows", "dimension", "numeric"])
def test_scatter_requires_dimension_two_measures_three_rows_and_one_numeric_pair(invalid):
    data = categorical_data(2)
    if invalid == "measures":
        data = categorical_data()
    elif invalid == "rows":
        data["rows"] = data["rows"][:2]
    elif invalid == "dimension":
        data["roles"]["d"]["role"] = "time"
    else:
        for row in data["rows"]:
            row[1] = None
            row[2] = "unavailable"
    assert "scatter" not in kinds(data)


def test_scatter_keeps_duplicate_members_missing_coordinates_and_all_result_rows():
    rows = [["甲", None, 2], ["甲", 2, None], *[["乙", i, i + 1] for i in range(22)]]
    chart = select(categorical_data(2, rows=rows), "scatter", top_n=2)
    assert len(chart["categories"]) == len(chart["points"]) == len(rows)
    assert chart["categories"][:3] == ["甲", "甲", "乙"]
    assert chart["points"][:2] == [
        {"name": "甲", "x": None, "y": 2.0, "size": None},
        {"name": "甲", "x": 2.0, "y": None, "size": None},
    ]
    assert chart["others"] is None


def test_scatter_three_rows_can_include_two_missing_coordinate_pairs():
    data = categorical_data(2, rows=[["甲", None, 2], ["乙", 2, None], ["丙", 3, 4]])
    chart = select(data, "scatter")
    assert len(chart["points"]) == 3
    assert chart["points"][-1] == {"name": "丙", "x": 3.0, "y": 4.0, "size": None}


def test_scatter_explicit_sort_applies_to_x_measure_or_category():
    data = categorical_data(2, rows=[["丙", 1, 8], ["甲", 3, 9], ["乙", 2, 4]])
    assert select(data, "scatter", sort="value_desc")["categories"] == ["甲", "乙", "丙"]
    assert select(data, "scatter", sort="category")["categories"] == ["丙", "乙", "甲"]


@pytest.mark.parametrize(
    "kind,factory",
    [("heatmap", heatmap_data), ("scatter", lambda: categorical_data(2)), ("waterfall", comparison_data)],
)
@pytest.mark.parametrize("marks", [[], ["max"], ["average"]])
def test_unsupported_marks_are_rejected_even_when_empty(kind, factory, marks):
    with pytest.raises(ValueError, match="marks"):
        select(factory(), kind, marks=marks)


@pytest.mark.parametrize("kind,factory", [("pareto", categorical_data), ("waterfall", comparison_data)])
@pytest.mark.parametrize("sort", ["category", "value_asc"])
def test_fixed_order_kinds_reject_conflicting_sort(kind, factory, sort):
    with pytest.raises(ValueError, match="sort|value_desc"):
        select(factory(), kind, sort=sort)


def test_waterfall_rejects_value_desc_and_pareto_accepts_it():
    with pytest.raises(ValueError, match="sort"):
        select(comparison_data(), "waterfall", sort="value_desc")
    assert select(categorical_data(), "pareto", sort="value_desc")["categories"] == ["m4", "m3", "m2", "m1"]


@pytest.mark.parametrize(
    "options", [{"top_n": 1}, {"top_n": 21}, {"top_n": True}, {"marks": ["median"]}, {"unknown": True}]
)
def test_new_kind_preference_retains_strict_schema_validation(options):
    with pytest.raises(ValidationError):
        ChartPreference(kind="pareto", title="结果观察", **options)


def test_pareto_highlight_checks_displayed_categories_and_unsupported_data_lists_choices():
    with pytest.raises(ValueError, match="highlight"):
        select(categorical_data(), "pareto", top_n=2, highlight=["m1"])
    with pytest.raises(ValueError, match="pareto"):
        select(temporal_data(), "pareto")


@pytest.mark.parametrize("kind", ["area", "combo", "pareto", "waterfall", "heatmap", "scatter"])
def test_tool_description_includes_each_new_kind_and_its_applicability(kind):
    assert kind in ChartPreference.tool_description()
    assert kind in ChartPreference.model_fields["kind"].description


@pytest.mark.parametrize("kind", ["area", "combo", "pareto", "waterfall", "heatmap", "scatter"])
def test_each_new_kind_exports_svg_and_mermaid_fallback_without_mutating_spec(kind):
    if kind == "area":
        data = temporal_data()
    elif kind == "combo":
        data = temporal_data(2)
        data["units"]["b"]["unit"] = "另一单位"
    elif kind == "waterfall":
        data = comparison_data()
    elif kind == "heatmap":
        data = heatmap_data()
    else:
        data = categorical_data(2 if kind == "scatter" else 1)
    spec = select(data, kind)
    assert ChartSpec.model_validate(spec).model_dump(mode="json") == spec
    before = deepcopy(spec)
    drawing = export._spec_chart(spec, "Helvetica")
    svg = renderSVG.drawToString(drawing)
    assert ElementTree.fromstring(svg).tag.endswith("svg")
    assert drawing.width > 0 and drawing.height > 0
    mermaid = export._mermaid_chart({"ref": "D1", "title": "结果观察", "chart": spec})
    assert isinstance(mermaid, list)
    if kind in ("area", "pareto"):
        assert "xychart-beta" in mermaid
    else:
        assert mermaid == []
    dataset = {**data, "column_roles": data["roles"], "ref": "D1"}
    assert figures.dataset_charts(dataset, {"kind": kind, "title": "结果观察"})[0] == spec
    assert spec == before
