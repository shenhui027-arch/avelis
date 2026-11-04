"""对话改图复用的偏好契约：严格工具校验与宽容读取共用确定性物化。"""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from avelis.analysis.tools import ChartArgs as AnalysisChartArgs
from avelis.ask.agent import ChartArgs as AskChartArgs
from avelis.charts import ChartPreference, recommend


def data(*, temporal=False, multiple=False, additive=False):
    columns = ["category", "value", *(["other"] if multiple else [])]
    rows = [["item10", 10], ["item2", 2], ["item1", 8]]
    if multiple:
        rows = [[*row, value] for row, value in zip(rows, [4, 12, 6], strict=True)]
    return {
        "columns": columns,
        "rows": rows,
        "roles": {
            "category": {"role": "time" if temporal else "dimension", "label": "类目"},
            "value": {"role": "measure", "label": "主度量"},
            **({"other": {"role": "measure", "label": "其他度量"}} if multiple else {}),
        },
        "units": {column: {"kind": "level" if additive else "ratio", "additive": additive} for column in columns[1:]},
        "polarity": {},
    }


def choice(**kwargs):
    return ChartPreference(kind="bar", title="已调整图表", **kwargs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("sort", "descending"),
        ("sort", 1),
        ("marks", ["median"]),
        ("marks", "max"),
        ("highlight", "item1"),
        ("highlight", [1]),
        ("highlight", list("abcdef")),
    ],
)
def test_invalid_optional_parameter_types_are_rejected_and_reading_ignores_whole_preference(field, value):
    with pytest.raises(ValidationError):
        choice(**{field: value})
    inputs = data(additive=True)
    assert recommend(**inputs, preference={"kind": "donut", "title": "不可应用", field: value}) == recommend(**inputs)


def test_both_agents_get_identical_optional_schema_and_help():
    shared = ChartPreference.model_json_schema()["properties"]
    assert AskChartArgs is ChartPreference
    assert {
        name: value for name, value in AnalysisChartArgs.model_json_schema()["properties"].items() if name != "dataset"
    } == shared
    assert {"sort", "marks", "highlight"} <= shared.keys()
    assert "算术平均" in ChartPreference.tool_description()


@pytest.mark.parametrize(
    "sort,categories,values",
    [
        ("value_desc", ["item10", "item1", "item2"], [10, 8, 2]),
        ("value_asc", ["item2", "item1", "item10"], [2, 8, 10]),
        ("category", ["item1", "item2", "item10"], [8, 2, 10]),
    ],
)
def test_sort_uses_selected_series_or_natural_display_order(sort, categories, values):
    inputs = data()
    before = deepcopy(inputs)
    chart = choice(sort=sort).materialize(**inputs)
    assert chart["categories"] == categories
    assert chart["series"][0]["values"] == values
    assert inputs == before


def test_category_sort_handles_numeric_labels_and_null_last():
    inputs = data()
    inputs["rows"] = [[-2, 1], [10, 2], [-10, 3], [None, 4]]
    assert choice(sort="category").materialize(**inputs)["categories"] == [-10, -2, 10, None]


@pytest.mark.parametrize("sort", ["value_desc", "value_asc", "category"])
def test_temporal_sort_rejected_by_tool_and_ignored_as_whole_by_recommendation(sort):
    inputs = data(temporal=True)
    with pytest.raises(ValueError, match="时间序列图不支持 sort"):
        ChartPreference(kind="line", title="时间图", sort=sort).materialize(**inputs)
    assert recommend(**inputs, preference={"kind": "line", "title": "不可应用", "sort": sort}) == recommend(**inputs)


def test_marks_materialize_positions_and_nonnull_displayed_value_average():
    inputs = data()
    inputs["rows"].append(["missing", None])
    chart = choice(marks=["max", "min", "average"], sort="category").materialize(**inputs)
    assert chart["marks"] == [
        {"type": "max", "series": "value", "series_index": 0, "x": "item10", "value": 10},
        {"type": "min", "series": "value", "series_index": 0, "x": "item2", "value": 2},
        {"type": "average", "series": "value", "series_index": 0, "x": None, "value": pytest.approx(20 / 3)},
    ]


def test_marks_use_each_series_and_measure_order():
    chart = ChartPreference(
        kind="grouped_bar",
        title="多系列",
        measures=["其他度量", "主度量"],
        marks=["max", "min", "average"],
    ).materialize(**data(multiple=True))
    assert [mark["series"] for mark in chart["marks"]] == ["other"] * 3 + ["value"] * 3
    assert [mark["series_index"] for mark in chart["marks"]] == [0] * 3 + [1] * 3
    assert chart["marks"][0]["x"] == "item2"
    assert chart["marks"][3]["x"] == "item10"
    assert chart["marks"][2]["value"] == pytest.approx(22 / 3)


def test_temporal_dimension_series_have_distinct_mark_indices_for_same_column():
    inputs = {
        "columns": ["month", "category", "value"],
        "rows": [["2026-01", "A", 3], ["2026-02", "A", 8], ["2026-01", "B", 9], ["2026-02", "B", 2]],
        "roles": {"month": {"role": "time"}, "category": {"role": "dimension"}, "value": {"role": "measure"}},
        "units": {},
        "polarity": {},
    }
    chart = ChartPreference(kind="line", title="时间图", marks=["max"]).materialize(**inputs)
    assert chart["marks"] == [
        {"type": "max", "series": "value", "series_index": 0, "x": "2026-02", "value": 8},
        {"type": "max", "series": "value", "series_index": 1, "x": "2026-01", "value": 9},
    ]


def test_highlight_matches_full_display_label_and_lists_valid_options():
    inputs = data()
    assert choice(highlight=["item2", "item2"]).materialize(**inputs)["highlight"] == ["item2"]
    with pytest.raises(ValueError, match="可选 highlight：.*item10.*item1.*item2"):
        choice(highlight=["unknown"]).materialize(**inputs)
    assert recommend(**inputs, preference={"kind": "bar", "title": "不可应用", "highlight": ["unknown"]}) == recommend(
        **inputs
    )
    inputs["rows"] = [[10, 1], [20, 2]]
    assert choice(highlight=["10"]).materialize(**inputs)["highlight"] == ["10"]


def test_top_n_sort_measures_marks_and_highlight_share_final_display_values():
    inputs = data(multiple=True)
    chart = choice(
        measures=["其他度量"], top_n=2, sort="value_asc", marks=["max", "average"], highlight=["item10"]
    ).materialize(**inputs)
    assert chart["categories"] == ["item10", "item1"]
    assert chart["series"][0]["values"] == [4, 6]
    assert chart["marks"] == [
        {"type": "max", "series": "other", "series_index": 0, "x": "item1", "value": 6},
        {"type": "average", "series": "other", "series_index": 0, "x": None, "value": 5},
    ]
    assert chart["highlight"] == ["item10"]
    with pytest.raises(ValueError, match="可选 highlight"):
        choice(measures=["value"], top_n=2, highlight=["item2"]).materialize(**inputs)


def test_marks_after_additive_top_n_include_other_bucket():
    chart = choice(top_n=2, marks=["average"]).materialize(**data(additive=True))
    assert chart["categories"] == ["item10", "item1", "其他"]
    assert chart["marks"][0]["value"] == pytest.approx(20 / 3)


def test_extreme_ties_use_first_displayed_position_and_empty_marks_have_no_points():
    inputs = data()
    inputs["rows"] = [["A", 4], ["B", 4], ["C", None]]
    chart = choice(marks=["max", "min", "max"]).materialize(**inputs)
    assert [mark["x"] for mark in chart["marks"]] == ["A", "A"]
    assert choice(marks=[]).materialize(**inputs)["marks"] == []


def test_strict_materialize_reports_available_kinds():
    with pytest.raises(ValueError, match="可选 kind：bar"):
        ChartPreference(kind="line", title="不可用").materialize(**data())


def test_marks_with_no_numeric_values_are_rejected_instead_of_silently_omitted():
    inputs = data()
    inputs["rows"] = [["A", None], ["B", "not numeric"]]
    with pytest.raises(ValueError, match="没有可标注的数值"):
        choice(marks=["max"]).materialize(**inputs)
    assert recommend(**inputs, preference={"kind": "bar", "title": "不可应用", "marks": ["max"]}) == recommend(**inputs)


def test_null_category_is_highlighted_by_its_rendered_label():
    """空类目在页面与导出里显示为“—”：highlight 按显示名匹配，不认 Python 的 "None"。"""
    inputs = data()
    inputs["rows"] = [["item1", 1], [None, 4]]
    assert choice(highlight=["—"]).materialize(**inputs)["highlight"] == ["—"]
    with pytest.raises(ValueError, match="可选 highlight：.*—"):
        choice(highlight=["None"]).materialize(**inputs)


@pytest.mark.parametrize(
    ("value", "label"), [(1.0, "1"), (2.5, "2.5"), (True, "true"), (None, "—"), ("华东", "华东"), (10, "10")]
)
def test_category_labels_match_the_browser_string_conversion(value, label):
    """highlight 按页面显示的类目名匹配：与 JS 的 String() 一致（String(1.0) 是 "1"、String(true) 是 "true"）。"""
    from avelis.charts import category_label

    assert category_label(value) == label


def test_integral_float_category_is_highlighted_by_its_browser_label():
    inputs = data()
    inputs["rows"] = [[1.0, 3], [2.0, 4]]
    assert choice(highlight=["1"]).materialize(**inputs)["highlight"] == ["1"]
    with pytest.raises(ValueError):
        choice(highlight=["1.0"]).materialize(**inputs)
