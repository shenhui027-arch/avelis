"""图表选择与物化结果的离线契约测试。"""

import json
from copy import deepcopy
from decimal import Decimal

import pytest
from pydantic import ValidationError

from avelis.analysis.dataset_projection import build_column_roles
from avelis.api.schemas import ChartSpec
from avelis.charts import recommend
from avelis.i18n import locale_context


def charts(columns, rows, *, dimensions=(), time=None, kinds=None, intent=None, units=None, polarity=None):
    kinds = kinds or {"value": "level"}
    units = units or {column: {"kind": kind, "unit": "元", "scale": 1} for column, kind in kinds.items()}
    roles = {column: {"role": "dimension", "label": column} for column in dimensions}
    if time:
        roles[time] = {"role": "time", "label": "月份", "grain": "month"}
    for column, kind in kinds.items():
        role = "change" if kind == "delta" else "rate" if kind == "rate_change" else "measure"
        roles[column] = {"role": role, "label": "成交额" if column == "value" else column}
    inputs = deepcopy((columns, rows, roles, units, polarity or {}, intent))
    result = recommend(columns, rows, roles, units, polarity or {}, intent)
    assert inputs == (columns, rows, roles, units, polarity or {}, intent)
    for spec in result:
        assert ChartSpec.model_validate(spec).model_dump(mode="json") == spec
        json.dumps(spec, allow_nan=False)
        assert set(spec) == {
            "kind",
            "orientation",
            "reason",
            "x",
            "categories",
            "series",
            "kpis",
            "others",
            "dropped_series",
        }
        if spec["kind"] != "kpi":
            assert all(len(s["values"]) == len(spec["categories"]) for s in spec["series"])
        if spec["reason"] != "time_by_dimension" or spec["others"] is None:
            assert all(s["other"] is False for s in spec["series"])
    return result


def test_kpi_without_comparison_numeric_parsing_and_metadata():
    result = charts(["value"], [[Decimal("80.25")]], polarity={"value": "up_good"})
    assert len(result) == 1
    spec = result[0]
    assert spec["kind"] == spec["reason"] == "kpi"
    assert spec["x"] is None and spec["categories"] == spec["series"] == []
    assert spec["others"] is None and spec["dropped_series"] == 0
    assert spec["kpis"] == [
        {
            "label": "成交额",
            "column": "value",
            "unit": {"kind": "level", "unit": "元", "scale": 1},
            "polarity": "up_good",
            "value": 80.25,
            "previous": None,
            "change": None,
            "rate": None,
            "previous_label": None,
        }
    ]


def comparison_columns(alias="销售额"):
    return [alias, "销售额·上一期", "销售额·变化", "销售额·变化率"]


def comparison_kinds(alias="销售额"):
    return dict(zip(comparison_columns(alias), ["level", "level", "delta", "rate_change"], strict=True))


@pytest.mark.parametrize("alias", ["销售额", "value", "sql_alias"])
def test_kpi_comparison_follows_column_order_including_sql_alias(alias):
    spec = charts(comparison_columns(alias), [[80, "300", -220, "-0.7333"]], kinds=comparison_kinds(alias))[0]
    assert len(spec["kpis"]) == 1
    kpi = spec["kpis"][0]
    assert (kpi["value"], kpi["previous"], kpi["change"], kpi["rate"]) == (80.0, 300.0, -220.0, -0.7333)
    assert kpi["previous_label"] == "上一期"


def test_multiple_comparison_groups_and_unpaired_measures_are_separate_kpis():
    columns = comparison_columns() + ["数量", "数量·去年同期", "数量·变化", "数量·变化率", "独立指标"]
    kinds = {
        **comparison_kinds(),
        "数量": "count",
        "数量·去年同期": "count",
        "数量·变化": "delta",
        "数量·变化率": "rate_change",
        "独立指标": "ratio",
    }
    spec = charts(columns, [[80, 300, -220, -0.7, 20, 10, 10, 1, 0.1]], kinds=kinds)[0]
    assert [k["column"] for k in spec["kpis"]] == ["销售额", "数量", "独立指标"]
    assert spec["kpis"][1]["previous_label"] == "去年同期"
    assert spec["kpis"][2]["previous"] is None


def test_incomplete_or_reordered_comparison_is_not_guessed():
    columns = comparison_columns()
    result = charts(columns[:2], [[80, 300]], kinds={c: "level" for c in columns[:2]})
    assert len(result[0]["kpis"]) == 2
    reordered = [columns[0], columns[2], columns[1], columns[3]]
    assert len(charts(reordered, [[80, -220, 300, -0.7]], kinds=comparison_kinds())[0]["kpis"]) == 2


def test_time_series_sorts_period_strings_without_changing_them():
    default, candidate = charts(["period", "value"], [["2026-7", "2"], ["2026-06", 1]], time="period")
    assert default["kind"] == "line" and default["reason"] == "time_series"
    assert default["x"] == {"column": "period", "label": "月份", "role": "time", "grain": "month"}
    assert default["categories"] == ["2026-06", "2026-7"]
    assert default["series"][0]["values"] == [1.0, 2.0]
    assert default["series"][0]["label"] == "成交额"
    assert candidate["kind"] == "bar" and candidate["orientation"] == "vertical"


@pytest.mark.parametrize("kind,candidate", [("level", "stacked_bar"), ("ratio", "grouped_bar")])
def test_time_by_dimension_keeps_members_and_missing_cells(kind, candidate):
    rows = [["02", "乙", 2], ["01", "甲", 1], ["02", "甲", 3]]
    default, alternate = charts(["t", "d", "value"], rows, dimensions=["d"], time="t", kinds={"value": kind})
    assert default["reason"] == "time_by_dimension"
    assert default["categories"] == ["01", "02"]
    assert [s["name"] for s in default["series"]] == ["乙", "甲"]
    assert [s["values"] for s in default["series"]] == [[None, 2.0], [1.0, 3.0]]
    assert alternate["kind"] == candidate


@pytest.mark.parametrize("kind", ["level", "count", "delta", "share", "ratio", "rate_change", "unknown"])
def test_time_by_many_members_merges_only_additive_units(kind):
    # 各期先求和再取绝对值；成员 8 的正负相抵，不能按各期绝对值求和入选。
    rows = [
        [period, f"m{i}", (-100 if period == "02" else 100) if i == 8 else i]
        for period in ("02", "01")
        for i in range(9)
    ]
    # rate 角色本身不能作为度量；用 measure 角色测试 unknown/rate_change 的可加性边界。
    roles = {
        "t": {"role": "time", "label": "时间"},
        "d": {"role": "dimension", "label": "维度"},
        "value": {"role": "measure", "label": "指标"},
    }
    result = recommend(["t", "d", "value"], rows, roles, {"value": {"kind": kind}}, {})
    default, candidate = result
    for spec in result:
        assert ChartSpec.model_validate(spec).model_dump(mode="json") == spec
        assert all(s["other"] is False for s in spec["series"][:7])
    assert [s["name"] for s in default["series"][:7]] == [f"m{i}" for i in range(7, 0, -1)]
    if kind in {"level", "count", "delta", "share"}:
        assert default["others"] == {"count": 2} and default["dropped_series"] == 0
        assert default["series"][-1]["name"] == "其他"
        assert default["series"][-1]["values"] == [100.0, -100.0]
        assert candidate["kind"] == "stacked_bar"
        assert default["series"][-1]["other"] is True
        assert candidate["series"][-1]["other"] is True
    else:
        assert len(default["series"]) == 7 and default["others"] is None
        assert default["dropped_series"] == 2 and candidate["kind"] == "grouped_bar"


@pytest.mark.parametrize("locale,other", [("en", "Other"), ("zh-TW", "其他")])
def test_other_flag_distinguishes_merged_series_from_business_member(locale, other):
    rows = [[t, other if i == 8 else f"m{i}", i] for t in ("01", "02") for i in range(9)]
    with locale_context(locale):
        result = charts(["t", "d", "value"], rows, dimensions=["d"], time="t")
    for spec in result:
        assert spec["series"][0]["name"] == spec["series"][-1]["name"] == other
        assert [s["other"] for s in spec["series"]] == [False] * 7 + [True]


def test_exactly_eight_members_are_not_reduced():
    rows = [[t, i, i] for t in ("01", "02") for i in range(8)]
    spec = charts(["t", "d", "value"], rows, dimensions=["d"], time="t")[0]
    assert len(spec["series"]) == 8 and spec["others"] is None


@pytest.mark.parametrize("temporal", [False, True])
def test_same_units_multi_measure(temporal):
    x = "t" if temporal else "d"
    default, *candidates = charts(
        [x, "a", "b"],
        [["02", 2, 4], ["01", 3, 5]],
        time=x if temporal else None,
        dimensions=[] if temporal else [x],
        kinds={"a": "level", "b": "level"},
    )
    assert default["reason"] == "multi_measure"
    assert default["kind"] == ("line" if temporal else "grouped_bar")
    assert default["orientation"] == ("vertical" if temporal else "horizontal")
    assert [s["name"] for s in default["series"]] == ["a", "b"]
    assert default["categories"] == ["01", "02"]
    assert len(candidates) == int(temporal)
    if temporal:
        assert candidates[0]["kind"] == "grouped_bar"


@pytest.mark.parametrize("temporal", [False, True])
@pytest.mark.parametrize("different", ["kind", "unit", "scale"])
@pytest.mark.parametrize("count", [2, 3, 4, 6])
def test_multiples_uses_one_panel_per_measure_and_caps_at_four(temporal, different, count):
    measures = list("abcdef")[:count]
    units = {
        "a": {"kind": "level", "unit": "元"},
        "b": {
            "kind": "ratio" if different == "kind" else "level",
            "unit": "件" if different == "unit" else "元",
            **({"scale": "10000"} if different == "scale" else {}),
        },
        **{column: {"kind": "count", "unit": "人"} for column in measures[2:]},
    }
    default, candidate = charts(
        ["x", *measures],
        [["02", *range(count)], ["01", *range(10, 10 + count)]],
        time="x" if temporal else None,
        dimensions=[] if temporal else ["x"],
        kinds={column: units[column]["kind"] for column in measures},
        units=units,
    )
    assert default["kind"] == "multiples" and default["orientation"] == "vertical"
    assert default["reason"] == candidate["reason"] == "multi_measure"
    assert default["dropped_series"] == max(0, count - 4)
    assert default["categories"] == candidate["categories"] == ["01", "02"]
    assert [s["column"] for s in default["series"]] == measures[:4]
    assert [(s["mark"], s["axis"]) for s in default["series"]] == [
        ("line" if temporal else "bar", index) for index in range(min(count, 4))
    ]
    assert [s["values"] for s in default["series"]] == [[10.0 + index, float(index)] for index in range(min(count, 4))]
    assert [s["unit"] for s in default["series"]] == [units[column] for column in measures[:4]]
    assert candidate["kind"] == ("line" if temporal else "bar") and len(candidate["series"]) == 1
    assert candidate["series"][0]["column"] == "a"
    assert candidate["series"][0]["mark"] == ("line" if temporal else "bar") and candidate["series"][0]["axis"] == 0
    assert candidate["series"][0]["values"] == [10.0, 0.0]


def test_chart_schema_defaults_other_to_false_and_rejects_combo():
    spec = charts(["t", "value"], [["01", 1], ["02", 2]], time="t")[0]
    del spec["series"][0]["other"]
    assert ChartSpec.model_validate(spec).series[0].other is False
    spec["kind"] = "combo"
    with pytest.raises(ValidationError, match="kind"):
        ChartSpec.model_validate(spec)


@pytest.mark.parametrize("order", [None, "asc", "desc"])
def test_ranking_sort_limit_and_other_total(order):
    default = charts(["d", "value"], [[i, i] for i in range(25)], dimensions=["d"], intent={"order": order})[0]
    expected = list(range(20)) if order == "asc" else list(range(24, 4, -1))
    assert default["kind"] == "bar" and default["orientation"] == "horizontal"
    assert default["categories"] == [*expected, "其他"]
    assert default["others"] == {"count": 5}
    assert default["series"][0]["values"][-1] == (110.0 if order == "asc" else 10.0)
    assert default["reason"] == ("composition" if order is None else "ranking")


@pytest.mark.parametrize(
    "kind,negative,candidate",
    [("level", False, True), ("share", False, True), ("ratio", False, False), ("level", True, False)],
)
def test_donut_only_for_additive_nonnegative_composition(kind, negative, candidate):
    result = charts(["d", "value"], [["a", -1 if negative else 1], ["b", 2]], dimensions=["d"], kinds={"value": kind})
    assert result[0]["reason"] == "composition"
    assert len(result) == (2 if candidate else 1)
    if candidate:
        assert result[1]["kind"] == "donut" and result[1]["series"] == result[0]["series"]
    assert charts(["d", "value"], [["a", 1]], dimensions=["d"], intent={"limit": 1})[0]["reason"] == "ranking"


def test_nonadditive_ranking_and_multiple_measures_do_not_merge_other_categories():
    rows = [[i, i, i + 1] for i in range(25)]
    for kinds, selected in [({"value": "ratio"}, [r[:2] for r in rows]), ({"value": "level", "other": "level"}, rows)]:
        spec = charts(["d", *kinds], selected, dimensions=["d"], kinds=kinds)[0]
        assert len(spec["categories"]) == 20 and spec["others"] is None
        assert spec["categories"] == list(range(24, 4, -1))


def test_comparison_dimension_precedes_time_and_sorts_each_chart_independently():
    columns = ["t", "d", *comparison_columns("value")]
    rows = [["01", "a", 80, 300, -220, -0.7], ["02", "b", 20, 10, 10, 1]]
    default, candidate = charts(
        columns, rows, time="t", dimensions=["d"], kinds=comparison_kinds("value"), polarity={"value": "down_good"}
    )
    assert default["kind"] == "diverging_bar" and default["orientation"] == "horizontal"
    assert default["reason"] == "change" and default["categories"] == ["b", "a"]
    assert default["series"][0]["values"] == [10.0, -220.0]
    assert default["series"][0]["polarity"] == "down_good"
    assert candidate["kind"] == "grouped_bar" and candidate["categories"] == ["a", "b"]
    assert [s["name"] for s in candidate["series"]] == ["本期", "上一期"]


def test_comparison_dimension_caps_each_chart_at_twenty_without_other():
    rows = [[i, i, 50 - i, i - 25, 0.1] for i in range(25)]
    result = charts(["d", *comparison_columns()], rows, dimensions=["d"], kinds=comparison_kinds())
    assert all(s["categories"] == list(range(24, 4, -1)) and s["others"] is None for s in result)


@pytest.mark.parametrize("kind", ["level", "ratio"])
def test_two_dimensions_flatten_single_measure(kind):
    rows = [["a", "x", 1], ["b", "y", 2], ["a", "y", 3]]
    result = charts(["d1", "d2", "value"], rows, dimensions=["d1", "d2"], kinds={"value": kind})
    spec = result[0]
    assert spec["kind"] == "bar" and spec["orientation"] == "horizontal"
    assert spec["categories"] == ["a / y", "b / y", "a / x"]
    assert [s["values"] for s in spec["series"]] == [[3.0, 2.0, 1.0]]
    assert len(result) == (2 if kind == "level" else 1)
    if kind == "level":
        assert result[1]["kind"] == "donut"


@pytest.mark.parametrize("dimension_count", [2, 3])
@pytest.mark.parametrize("mixed_units", [False, True])
def test_multiple_dimensions_and_measures_keep_every_dimension(dimension_count, mixed_units):
    dimensions = ["区域", "渠道", "产品"][:dimension_count]
    members = [["北", "线上", "甲"], ["北", "线下", "甲"], ["北", "线上", "乙"]]
    if dimension_count == 2:
        members = members[:2]
    rows = [[*member[:dimension_count], 100 * (i + 1), 0.1 * (i + 1)] for i, member in enumerate(members)]
    result = charts(
        [*dimensions, "金额", "转化率"],
        rows,
        dimensions=dimensions,
        kinds={"金额": "level", "转化率": "ratio" if mixed_units else "level"},
    )
    expected = list(reversed(rows))
    for spec in result:
        assert spec["categories"] == [" / ".join(row[:dimension_count]) for row in expected]
    assert result[0]["kind"] == ("multiples" if mixed_units else "grouped_bar")
    assert [s["values"] for s in result[0]["series"]] == [
        [float(row[dimension_count + i]) for row in expected] for i in range(2)
    ]


def test_multiple_dimensions_comparison_keeps_member_combinations():
    result = charts(
        ["区域", "渠道", *comparison_columns()],
        [["北", "线上", 10, 8, 2, 0.25], ["北", "线下", 30, 20, 10, 0.5]],
        dimensions=["区域", "渠道"],
        kinds=comparison_kinds(),
    )
    assert all(spec["categories"] == ["北 / 线下", "北 / 线上"] for spec in result)


def test_many_second_dimension_members_flatten_categories():
    spec = charts(["d1", "d2", "value"], [["a", i, i] for i in range(7)], dimensions=["d1", "d2"])[0]
    assert spec["kind"] == "bar"
    assert spec["categories"] == [f"a / {i}" for i in range(6, -1, -1)]


def test_empty_or_no_measure_and_unsupported_shapes():
    assert recommend(["value"], [], {"value": {"role": "measure"}}, {}, {}) == []
    assert recommend(["d"], [["a"]], {"d": {"role": "dimension"}}, {}, {}) == []
    assert charts(["value"], [[1], [2]]) == []
    assert charts(["r"], [[0.2]], kinds={"r": "rate_change"}) == []


def test_single_period_falls_back_to_dimension_but_is_not_kpi():
    assert charts(["t", "value"], [["01", 1]], time="t") == []
    spec = charts(["t", "d", "value"], [["01", "a", 1], ["01", "b", 2]], time="t", dimensions=["d"])[0]
    assert spec["kind"] == "bar" and spec["x"]["column"] == "d"
    assert spec["categories"] == ["b", "a"]


def test_change_without_measure_can_be_displayed():
    spec = charts(["d", "diff"], [["a", -1], ["b", 2]], dimensions=["d"], kinds={"diff": "delta"})[0]
    assert spec["kind"] == "bar" and spec["series"][0]["column"] == "diff"


@pytest.mark.parametrize("raw", [None, True, "NaN", "Infinity", "1e999", "text"])
def test_invalid_and_nonfinite_values_are_null(raw):
    spec = recommend(["value"], [[raw]], {"value": {"role": "measure", "label": "值"}}, {}, {})[0]
    assert spec["kpis"][0]["value"] is None
    assert spec["kpis"][0]["unit"] == {"kind": "unknown"}
    json.dumps(spec, allow_nan=False)


def test_nulls_sort_last_in_both_directions_and_stable_ties():
    for order in ("asc", "desc"):
        spec = charts(
            ["d", "value"], [["missing", None], ["first", 1], ["second", 1]], dimensions=["d"], intent={"order": order}
        )[0]
        assert spec["categories"] == ["first", "second", "missing"]
        assert spec["series"][0]["values"] == [1.0, 1.0, None]


@pytest.mark.parametrize(
    "locale,other,current,previous,month",
    [("en", "Other", "Current period", "Previous period", "Month"), ("zh-TW", "其他", "本期", "上一期", "月份")],
)
def test_system_labels_translate_and_business_values_stay_original(locale, other, current, previous, month):
    with locale_context(locale):
        roles = build_column_roles(
            ["t", "value"],
            {"time_column": "t", "intent": {"time_grain": "month"}, "units": {"value": {"kind": "level"}}},
        )
        assert roles["t"] == {"role": "time", "label": month, "grain": "month"}
        rows = [[i, i] for i in range(25)]
        assert charts(["d", "value"], rows, dimensions=["d"])[0]["categories"][-1] == other
        result = charts(
            ["d", *comparison_columns()],
            [["甲", 1, 2, -1, -0.5], ["乙", 2, 1, 1, 1]],
            dimensions=["d"],
            kinds=comparison_kinds(),
        )
        assert [s["name"] for s in result[1]["series"]] == [current, previous]
        assert set(result[0]["categories"]) == {"甲", "乙"}


@pytest.mark.parametrize(
    "grain,label",
    [(None, "t"), ("day", "日期"), ("week", "周"), ("month", "月份"), ("quarter", "季度"), ("year", "年")],
)
def test_column_roles_time_labels_grain_and_explicit_override(grain, label):
    meta = {
        "time_column": "t",
        "intent": {"time_grain": grain, "group_by": ["d"]},
        "units": {"v": {"kind": "share"}, "diff": {"kind": "delta"}, "r": {"kind": "rate_change"}},
    }
    columns = ["t", "d", "v", "diff", "r"]
    roles = build_column_roles(columns, meta)
    assert roles["t"] == {"role": "time", "label": label, "grain": grain}
    assert roles["d"] == {"role": "dimension", "label": "d"}
    assert roles["v"]["additive"] is True and roles["diff"]["additive"] is True
    assert roles["r"]["additive"] is False
    meta["column_labels"] = {"t": "指定时间"}
    assert build_column_roles(columns, meta)["t"]["label"] == "指定时间"


def test_time_series_split_by_every_dimension():
    rows = [["01", "甲", "a", 1], ["01", "甲", "b", 2], ["02", "甲", "a", 3], ["02", "甲", "b", 4]]
    default = charts(["period", "d1", "d2", "value"], rows, dimensions=("d1", "d2"), time="period")[0]
    assert default["kind"] == "line"
    assert [s["name"] for s in default["series"]] == ["甲 / a", "甲 / b"]
    assert [s["values"] for s in default["series"]] == [[1.0, 3.0], [2.0, 4.0]]


def test_structured_dimension_values_become_stable_text_keys():
    rows = [[["x", "y"], 3], [{"k": 1}, 2], [["x", "y"], 1]]
    default = charts(["d", "value"], rows, dimensions=("d",))[0]
    assert default["categories"] == ['["x", "y"]', '{"k": 1}']
    assert default["series"][0]["values"] == [4.0, 2.0]


def test_dimension_combinations_with_separator_text_do_not_collide():
    rows = [["a / b", "c", 10], ["a", "b / c", 30]]
    default = charts(["d1", "d2", "value"], rows, dimensions=("d1", "d2"))[0]
    assert default["categories"] == ["a / b / c", "a / b / c"]
    assert default["series"][0]["values"] == [30.0, 10.0]
