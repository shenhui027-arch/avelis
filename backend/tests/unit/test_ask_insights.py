"""确定性回答要点；内存结果与假会话，不访问数据库、缓存服务或模型。"""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from avelis import config
from avelis.analysis import grounding
from avelis.api.ask import _answer_result
from avelis.ask import insights, service
from avelis.ask.intent import Intent
from avelis.i18n import locale_context


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    settings = config.build_settings({"AVELIS_SECRET": "unit-test-secret-unit-test-secret"})
    for module in (grounding, service):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    with locale_context("zh-CN"):
        yield settings


def data(rows=None, *, kind="level", additive=True, time=False):
    column = "value"
    return {
        "columns": ["period" if time else "member", column],
        "rows": rows if rows is not None else [["甲", 30], ["乙", 20], ["丙", 10]],
        "column_roles": {
            "period" if time else "member": {"role": "time" if time else "dimension"},
            column: {"role": "measure", "label": "度量"},
        },
        "units": {column: {"kind": kind, "unit": "项" if kind == "level" else "", "additive": additive}},
        "polarity": {column: "down_good"},
    }


def by_kind(result):
    return {item["kind"]: item for item in insights.build(**result)}


def test_total_peak_low_share_are_ordered_and_use_first_measure_only():
    result = data()
    result["columns"].append("second")
    result["column_roles"]["second"] = {"role": "measure"}
    result["units"]["second"] = {"kind": "level", "additive": True}
    result["rows"] = [[*row, 100000] for row in result["rows"]]
    before = deepcopy(result)
    items = insights.build(**result)
    assert [item["kind"] for item in items] == ["total", "peak", "low", "share"]
    assert items[0] == {"kind": "total", "text": "度量合计为60项。", "column": "value", "category": None, "value": 60}
    assert items[1]["text"] == "甲的度量最高，为30项。"
    assert items[1]["category"] == "甲" and items[1]["value"] == 30
    assert items[2]["text"] == "丙的度量最低，为10项。"
    assert items[3]["text"] == "甲的度量占合计的50.0%。"
    assert items[3]["value"] == 0.5
    assert result == before


def test_share_reports_minimum_top_n_reaching_half_and_rejects_negative_or_unknown_values():
    result = data([["甲", 4], ["乙", 3], ["丙", 2], ["丁", 1]])
    share = by_kind(result)["share"]
    assert share["text"] == "甲的度量占合计的40.0%；前 2 项合计占70.0%。"
    for value in (-1, None, "NaN", "Infinity"):
        result["rows"][-1][1] = value
        assert "share" not in by_kind(result)
    assert "share" not in by_kind(data([["甲", 0], ["乙", 0], ["丙", 0]]))


@pytest.mark.parametrize("time", [False, True])
def test_member_aggregation_keeps_raw_dimensions_with_identical_display_labels_separate(time):
    result = data(kind="ratio" if time else "level", additive=not time, time=time)
    result["columns"] = [*(["period"] if time else []), "member", "other", "value"]
    result["column_roles"].update({"member": {"role": "dimension"}, "other": {"role": "dimension"}})
    result["rows"] = [
        [*(["2026-01"] if time else []), "A / B", "C", 0.6 if time else 6],
        [*(["2026-01"] if time else []), "A", "B / C", 0.4 if time else 4],
    ]
    item = by_kind(result)["spread" if time else "share"]
    assert item["value"] == (0.2 if time else 0.6)
    assert item["text"] == (
        "2026-01的成员间差距最大，A / B / C与A / B / C的度量相差20.0个百分点。"
        if time
        else "A / B / C的度量占合计的60.0%。"
    )


def test_decimal_total_preserves_small_value_between_large_opposite_values():
    total = by_kind(data([["甲", "6e33"], ["乙", 1], ["丙", "-6e33"]]))["total"]
    assert total["value"] == 1
    assert total["text"] == "度量合计为1项。"


@pytest.mark.parametrize("kind", ["ratio", "share", "rate_change"])
def test_ratios_are_not_totalled_even_if_additive_is_incorrectly_set(kind):
    items = by_kind(data([["甲", 0.2], ["乙", 0.5], ["丙", 0.4]], kind=kind))
    assert "total" not in items and "share" not in items
    assert set(items) == {"peak", "low"}


def test_time_change_uses_natural_period_order_and_shared_delta_units():
    result = data([["2026-03", 10], ["2026-01", 20], ["2026-02", 35]], time=True)
    items = insights.build(**result)
    assert [item["kind"] for item in items] == ["total", "change", "peak", "low"]
    change = items[1]
    assert change["category"] == "2026-03" and change["value"] == -10
    assert change["text"] == "度量从2026-01的20项变为2026-03的10项，变化-10项，变化率为-50.0%。"


def test_zero_base_and_non_additive_ratio_change_are_formatted_without_summing():
    result = data([["2026-01", 0], ["2026-02", 0.25]], kind="ratio", additive=False, time=True)
    change = by_kind(result)["change"]
    assert change["text"] == "度量从2026-01的0.0%变为2026-02的25.0%，变化+25.0个百分点；基期为零，无法计算变化率。"
    result["rows"][0][1] = None
    assert "change" not in by_kind(result)


def comparison_data(*, additive=True):
    columns = ["member", "value", "度量·上一期", "度量·变化", "度量·变化率"]
    roles = dict(
        zip(columns, ({"role": role} for role in ("dimension", "measure", "measure", "change", "rate")), strict=True)
    )
    roles["value"]["label"] = "度量"
    return {
        "columns": columns,
        "rows": [["甲", 30, 20, 10, 0.5], ["乙", 10, 20, -10, -0.5]],
        "column_roles": roles,
        "units": {
            "value": {"kind": "level" if additive else "ratio", "unit": "项" if additive else "", "additive": additive},
            "度量·上一期": {"kind": "level", "additive": additive},
            "度量·变化": {"kind": "delta", "additive": additive},
            "度量·变化率": {"kind": "rate_change", "additive": False},
        },
        "polarity": {},
    }


def test_comparison_uses_current_and_previous_totals_without_totaling_other_columns():
    result = comparison_data()
    items = by_kind(result)
    assert items["total"]["value"] == 40
    assert items["change"]["value"] == 0
    assert items["change"]["text"] == "度量从上一期的40项变为本期的40项，变化0项，变化率为0.0%。"
    result["rows"][0][2] = None
    assert "change" not in by_kind(result)


def test_non_additive_comparison_reports_one_member_using_existing_comparison_values():
    result = comparison_data(additive=False)
    result["rows"] = [["甲", 0.3, 0.2, 0.1, 0.5], ["乙", 0.1, 0.4, -0.3, -0.75]]
    change = by_kind(result)["change"]
    assert change["category"] == "乙" and change["value"] == -0.3
    assert change["text"] == "乙的度量从上一期的40.0%变为本期的10.0%，变化-30.0个百分点，变化率为-75.0%。"
    assert "total" not in by_kind(result)


def test_spread_is_reachable_for_non_additive_time_member_data():
    result = data(kind="ratio", additive=False, time=True)
    result["columns"].insert(1, "member")
    result["column_roles"]["member"] = {"role": "dimension"}
    result["rows"] = [["2026-01", "甲", 0.2], ["2026-01", "乙", 0.3], ["2026-02", "甲", 0.8], ["2026-02", "乙", 0.1]]
    items = by_kind(result)
    assert list(items) == ["peak", "low", "spread"]
    assert items["peak"]["category"] == "2026-02 / 甲"
    assert items["spread"]["value"] == 0.7
    assert items["spread"]["category"] == "2026-02"
    assert "相差70.0个百分点" in items["spread"]["text"]
    assert "change" not in items  # 多成员比率不能合成为期间总体比率。


@pytest.mark.parametrize("rows", [[], [["甲", 1]], [["甲", None], ["乙", "not numeric"]]])
def test_empty_single_and_all_non_numeric_rows_have_no_insights(rows):
    assert insights.build(**data(rows)) == []


def test_missing_contract_duplicate_columns_and_short_rows_are_safe():
    result = data([[True, 2], [None], [1.0, 3]])
    items = by_kind(result)
    assert items["peak"]["category"] == "1"
    assert items["low"]["category"] == "true"
    assert "share" not in items
    result["column_roles"] = {}
    assert insights.build(**result) == []
    result["columns"] = ["value", "value"]
    assert insights.build(**result) == []


def test_two_rows_omit_low_and_non_additive_levels_omit_total():
    assert list(by_kind(data([["甲", 3], ["乙", 2]]))) == ["total", "peak", "share"]
    assert list(by_kind(data(additive=False))) == ["peak", "low"]


def test_measure_formatting_reuses_scale_number_style_and_polarity_is_not_a_cause(monkeypatch, settings):
    result = data([["甲", 10000], ["乙", 20000], ["丙", 30000]])
    result["units"]["value"]["scale"] = "10000"
    assert by_kind(result)["total"]["text"] == "度量合计为6.00亿项。"
    monkeypatch.setattr(grounding, "get_settings", lambda: replace(settings, number_style="intl"))
    assert by_kind(result)["total"]["text"] == "度量合计为600.00M项。"
    assert "好" not in by_kind(result)["peak"]["text"]


def metric_objects():
    metric = SimpleNamespace(id="metric", code="metric", name="度量")
    version = SimpleNamespace(
        id="locked", metric_id=metric.id, kind="base", expression="SUM(v)", value_kind="level", unit="项", dimensions=[]
    )
    return metric, version


def test_result_contract_populates_insights_on_fresh_and_cached_fulfil_without_mutating_cached_rows(monkeypatch):
    metric, version = metric_objects()
    intent = Intent(metric_code="metric", group_by=["member"])
    raw = {"columns": ["member", "value"], "rows": [["甲", 3], ["乙", 2]], "truncated": False, "row_count": 2}
    before = deepcopy(raw)
    for cached in (False, True):
        answer = SimpleNamespace(status="answered", result=raw, cache_hit=cached)
        monkeypatch.setattr(service, "_fulfil", lambda *args, **kwargs: None)
        service.fulfil(None, None, intent, answer, service.Catalog([], {"metric": (metric, version)}))
        assert answer.result["insights"][0]["value"] == 5
        assert answer.cache_hit == cached
        assert raw == before


def test_api_replay_adds_insights_beside_charts_without_mutating_saved_answer():
    result = data()
    result["insights"] = [{"text": "stale"}]
    answer = SimpleNamespace(status="answered", result=result, intent={}, evidence={})
    before = deepcopy(result)
    projected = _answer_result(answer, None)
    assert projected["insights"] == insights.build(**{key: result[key] for key in data()})
    assert projected["charts"] and projected["insights"][0]["value"] == 60
    assert answer.result == before


def test_api_replays_old_result_from_locked_version_using_fake_session():
    metric, version = metric_objects()
    answer = SimpleNamespace(
        status="answered",
        result={"columns": ["member", "value"], "rows": [["甲", 3], ["乙", 2]]},
        intent=Intent(metric_code="metric", group_by=["member"]).model_dump(mode="json"),
        metric_version_ids=["locked"],
        evidence={},
    )
    session = Mock()
    session.get.side_effect = lambda cls, key: {"locked": version, "metric": metric}[key]
    projected = _answer_result(answer, session)
    assert projected["insights"][0]["value"] == 5 and projected["charts"]
    assert "column_roles" not in answer.result and "insights" not in answer.result


@pytest.mark.parametrize("value", ["1e1000000", "1e50", "1e308"])
def test_unrepresentable_values_leave_insights_empty_without_breaking_answer_projection(value):
    result = data([["甲", value], ["乙", 1]])
    assert insights.build(**result) == []
    answer = SimpleNamespace(status="answered", result=result, intent={}, evidence={})
    assert _answer_result(answer, None)["insights"] == []


def test_unrepresentable_total_is_skipped_while_other_insights_remain_available():
    items = by_kind(data([["甲", "6e33"], ["乙", "6e33"], ["丙", 0]]))
    assert list(items) == ["peak", "low", "share"]
    assert items["share"]["value"] == 0.5


@pytest.mark.parametrize("previous", ["1e1000000", "1e50", "1e308"])
def test_unrepresentable_comparison_period_skips_change_without_breaking_total(previous):
    result = comparison_data()
    result["rows"][0][2] = previous
    items = by_kind(result)
    assert "change" not in items
    assert items["total"]["value"] == 40


def test_extremely_small_base_only_skips_overflowing_change_rate():
    items = by_kind(data([["2026-01", "1e-1000000"], ["2026-02", 1]], time=True))
    assert list(items) == ["total", "peak"]
    assert items["total"]["value"] == 1


def test_unrepresentable_derived_change_is_skipped_while_extrema_remain_available():
    items = by_kind(data([["2026-01", "-6e33"], ["2026-02", 0], ["2026-03", "6e33"]], time=True))
    assert list(items) == ["total", "peak", "low"]
