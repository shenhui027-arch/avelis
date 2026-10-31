"""核心指标只采用与报告已知筛选、日期窗口相容的结果。"""

from copy import deepcopy

import pytest

from avelis.analysis import export
from tests.unit.test_analysis_export import sample_view


def _view(scope, first_intent):
    view = sample_view()
    first = view["datasets"][0]
    first["meta"]["metric_codes"] = ["metric_a"]
    second = deepcopy(first)
    first["meta"]["intent"] = first_intent
    first["rows"] = [["30", "20", "10", "0.5"]]
    second.update(ref="D2", seq=2, rows=[["222", "111", "111", "1"]])
    second["meta"]["intent"] = {"filters": scope.get("filters", []),
                                  "start_date": "2026-09-01", "end_date": "2026-09-30",
                                  "compare": "custom", "compare_start": "2026-08-01",
                                  "compare_end": "2026-08-31"}
    view["datasets"] = [first, second]
    view["report"] = {"metric_code": "metric_a", "scope": scope,
                      "claims": [{"type": "fact", "refs": ["D1", "D2"]}]}
    return view


def _assert_ref(view, expected):
    metrics = export._core_metrics(view)
    assert {metric["ref"] for metric in metrics} == {expected}
    assert metrics[0]["raw"] == (222 if expected == "D2" else 30)


@pytest.mark.parametrize(("scope_filters", "first_filters", "expected"), [
    ([{"dimension": "dimension_a", "values": ["member_a"]}],
     [{"dimension": "dimension_a", "values": ["member_b"]}], "D2"),
    ([{"dimension": "dimension_a", "values": ["member_a"]}], [], "D2"),
    ([], [{"dimension": "dimension_a", "values": ["member_a"]}], "D2"),
    ([{"dimension": "dimension_a", "values": ["member_a", "member_b"]},
      {"dimension": "dimension_b", "values": ["member_c"]}],
     [{"dimension": "dimension_b", "values": ["member_c"]},
      {"dimension": "dimension_a", "values": ["member_b", "member_a", "member_a"]}], "D1"),
])
def test_core_metrics_match_normalized_filters(scope_filters, first_filters, expected):
    _assert_ref(_view({"filters": scope_filters}, {"filters": first_filters}), expected)


@pytest.mark.parametrize(("key", "different"), [
    ("start_date", "2026-09-02"), ("end_date", "2026-09-29"),
    ("compare_start", "2026-08-02"), ("compare_end", "2026-08-30"),
])
def test_core_metrics_skip_a_different_current_or_comparison_window(key, different):
    scope = {"start_date": "2026-09-01", "end_date": "2026-09-30",
             "compare_start": "2026-08-01", "compare_end": "2026-08-31"}
    _assert_ref(_view(scope, {**scope, key: different}), "D2")


@pytest.mark.parametrize(("compare", "compare_start", "compare_end", "expected"), [
    ("previous_period", "2026-08-02", "2026-08-31", "D1"),
    ("previous_period", "2026-08-01", "2026-08-31", "D2"),
    ("previous_year", "2025-09-01", "2025-09-30", "D1"),
    ("previous_year", "2026-08-01", "2026-08-31", "D2"),
])
def test_core_metrics_resolve_relative_comparison_windows(compare, compare_start, compare_end, expected):
    scope = {"start_date": "2026-09-01", "end_date": "2026-09-30",
             "compare_start": compare_start, "compare_end": compare_end}
    first_intent = {"start_date": scope["start_date"], "end_date": scope["end_date"], "compare": compare}
    _assert_ref(_view(scope, first_intent), expected)


def test_core_metrics_use_saved_window_when_intent_dates_are_missing():
    view = _view({"start_date": "2026-09-01"}, {})
    view["datasets"][0]["meta"]["window"]["start"] = "2026-08-01"
    _assert_ref(view, "D2")


@pytest.mark.parametrize(("scope", "first_intent"), [
    ({}, {"filters": [{"dimension": "dimension_a", "values": ["member_b"]}]}),
    ({"filters": [{"dimension": "dimension_a", "values": ["member_a"]}]}, {}),
    ({"filters": [{"dimension": "dimension_a", "values": ["member_a"]}]}, {"filters": None}),
    ({"compare_start": "2026-08-01", "compare_end": "2026-08-31"}, {}),
    ({"start_date": "2026-09-01", "end_date": "2026-09-30"}, {}),
])
def test_core_metrics_keep_original_selection_when_scope_information_is_missing(scope, first_intent):
    view = _view(scope, first_intent)
    view["datasets"][0]["meta"].pop("window", None)
    _assert_ref(view, "D1")


def test_core_metrics_do_not_fall_back_to_an_explicitly_different_scope():
    view = _view({"filters": [{"dimension": "dimension_a", "values": ["member_a"]}]},
                 {"filters": [{"dimension": "dimension_a", "values": ["member_b"]}]})
    view["datasets"] = view["datasets"][:1]
    assert export._core_metrics(view) == []
