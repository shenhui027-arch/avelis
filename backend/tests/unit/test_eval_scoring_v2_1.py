"""评分器 v2.1 的离线规则回归：运行记录与真值均由测试手写，不读取评测样本。

机制要同时证明资产与成员，避免标签碰巧命中；探索数值要证明实体和完整期间，避免同值误认。
"""

from copy import deepcopy
from decimal import Decimal

import pytest

from tests.eval import scoring_v2_1 as v21

WINDOW = {"start": "2026-09-01", "end": "2026-09-30",
          "compare_start": "2026-08-01", "compare_end": "2026-08-31"}
DOUYIN = [{"asset": "ec_marketing.ad_spend_daily", "members": ["douyin"], "why": "渠道投放削减"},
          {"asset": "ec_trade.orders", "members": ["douyin"], "why": "付费订单随投放减少"}]
PAID = [{"asset": "ec_marketing.ad_spend_daily",
         "members": ["douyin", "xiaohongshu", "wechat_ads", "search"], "why": "付费预算回落"},
        {"asset": "ec_trade.orders",
         "members": ["douyin", "xiaohongshu", "wechat_ads", "search"], "why": "付费订单回落"}]


def dataset(ref="D1", *, sql=None, columns=None, rows=None, kind="explore", inputs=None):
    """保留接口实际需要的最小字段，不依赖演示环境。"""
    rows = rows if rows is not None else [["douyin", 100]]
    return {"ref": ref, "kind": kind, "title": "手写数据", "sql": sql or "SELECT * FROM ec_dw.dwd_order",
            "columns": columns or ["source_channel", "orders"], "rows": rows, "row_count": len(rows),
            "inputs": inputs or []}


def cause(entity=None, *, evidence=None, direction="down", status="verified", label="证据"):
    return {"type": "member", "direction": direction, "status": status, "label": label,
            "entity": entity or {"evidence_id": "D1#1"}, "evidence": evidence or ["D1"]}


def run_record(datasets=None, *, causes=None, steps=None, segment=None, conclusion="concentrated"):
    report = {"conclusion": conclusion, "causes": causes or [], "fields": {}}
    if segment is not None:
        report["fields"]["summary"] = {"segments": [segment]}
    return {"status": "succeeded", "window": dict(WINDOW), "report": report, "verification": {},
            "datasets": datasets or [], "steps": steps or []}


def mechanism_question(alternatives=None):
    return {"id": "d02", "category": "dimension", "window": dict(WINDOW),
            "expect": {"conclusion": ["concentrated"],
                       "primary": {"types": ["member", "factor"], "direction": "down", "entity": ["抖音"]}},
            "mechanism": deepcopy(alternatives if alternatives is not None else DOUYIN)}


def metric_question(*, entity=None, absolute=False, tolerance=0):
    return {"id": "d02", "category": "dimension", "window": dict(WINDOW),
            "expect": {"key_value": {"value": 100, "tolerance": tolerance, "abs": absolute}},
            "v2": {"key_bindings": [{"metric": "ec_ad_orders",
                                     "entity": entity if entity is not None else {"region": "华东", "channel": "抖音"},
                                     "truth": {"current": Decimal(100), "compare": Decimal(120),
                                               "change": Decimal(-20)}}]}}


def value_segment(path="D1.orders[#1]", raw=100, **extra):
    return {"kind": "value", "path": path, "raw": raw, **extra}


def key_result(sql, *, columns=None, rows=None, q=None, path="D1.orders[#1]", raw=100, **extra):
    run = run_record([dataset(sql=sql, columns=columns, rows=rows)],
                     segment=value_segment(path, raw, **extra))
    return v21.key_value(q or metric_question(), run["report"], run)


def test_d02_evidence_row_uses_order_view_lineage_and_dictionary_alias():
    first = cause()
    run = run_record([dataset()], causes=[first])
    detail = v21.match_mechanism(first, run, DOUYIN)
    result = v21.score(mechanism_question(), run)
    assert detail["hit"] is True
    assert detail["alternative"] == 2
    assert result["primary_hit"] is True
    assert result["primary_detail"]["alternative"] == 2
    assert result["mechanism"] == result["primary_detail"]


def test_governed_member_uses_marketing_view_lineage():
    first = cause({"dimension": "channel", "member": "抖音"})
    source = dataset(kind="governed", sql="-- 本期\nSELECT * FROM ec_dw.dws_marketing_daily\n"
                     "-- 对比期\nSELECT * FROM ec_dw.dws_marketing_daily")
    detail = v21.match_mechanism(first, run_record([source]), DOUYIN)
    assert detail["hit"] is True
    assert detail["alternative"] == 1


@pytest.mark.parametrize("member,sql,label", [
    ("douyin", "SELECT * FROM ec_service.service_ticket", "抖音投放减少"),
    ("organic", "SELECT * FROM ec_dw.dwd_order", "抖音投放减少"),
    ("douyin_extra", "SELECT * FROM ec_dw.dwd_order", "抖音投放减少"),
])
def test_member_and_asset_must_both_match_exact_cells(member, sql, label):
    first = cause(label=label)
    run = run_record([dataset(sql=sql, rows=[[member, 100]])], causes=[first])
    result = v21.score(mechanism_question(), run)
    assert result["primary_hit"] is False
    assert result["primary_detail"].get("reason")


def derived_run(channels, *, values=None):
    source = dataset(rows=[[name, 10] for name in channels])
    result = dataset("D2", kind="calc", columns=["name", "dataset", "column", "rows", "value"],
                     rows=[["selected", "D1", "orders", len(channels), 10 * len(channels)],
                           ["result", None, None, None, 10 * len(channels)]], inputs=["D1"])
    step = {"dataset": "D2", "tool": "derive", "args": {
        "values": values or [{"name": "selected", "dataset": "D1", "column": "orders", "match": {}}],
        "formula": "selected"}}
    first = cause({"evidence_id": "D2#2"}, evidence=["D2"])
    return first, run_record([source, result], causes=[first], steps=[step])


@pytest.mark.parametrize("channels,hit", [
    (["douyin", "organic"], False),
    (["douyin", "xiaohongshu", "wechat_ads", "search"], True),
])
def test_h07_derived_aggregate_requires_every_input_row_to_be_paid(channels, hit):
    first, run = derived_run(channels)
    assert v21.match_mechanism(first, run, PAID)["hit"] is hit


@pytest.mark.parametrize("match,exclude", [({"source_channel": "douyin"}, False),
                                         ({"source_channel": "organic"}, True)])
def test_derive_uses_match_and_exclude_before_testing_members(match, exclude):
    values = [{"name": "selected", "dataset": "D1", "column": "orders", "match": match, "exclude": exclude}]
    first, run = derived_run(["douyin", "organic"], values=values)
    assert v21.match_mechanism(first, run, PAID)["hit"] is True


def test_derive_recurses_to_selected_source_rows():
    _, run = derived_run(["douyin"])
    run["datasets"].append(dataset("D3", kind="calc", inputs=["D2"],
                                  columns=["name", "dataset", "column", "rows", "value"],
                                  rows=[["nested", "D2", "value", 1, 10], ["result", None, None, None, 10]]))
    run["steps"].append({"dataset": "D3", "tool": "derive", "args": {
        "values": [{"name": "nested", "dataset": "D2", "column": "value", "match": {"name": "selected"}}],
        "formula": "nested"}})
    first = cause({"evidence_id": "D3#2"}, evidence=["D3"])
    assert v21.match_mechanism(first, run, PAID)["hit"] is True


def test_derive_cycle_stops_at_depth_limit():
    first, run = derived_run(["douyin"])
    run["steps"][0]["args"]["values"][0]["dataset"] = "D2"
    detail = v21.match_mechanism(first, run, PAID)
    assert detail["hit"] is False
    assert detail.get("reason")


@pytest.mark.parametrize("number", [0, 2, 50])
def test_evidence_row_outside_saved_rows_has_reason(number):
    first = cause({"evidence_id": f"D1#{number}"})
    detail = v21.match_mechanism(first, run_record([dataset()]), DOUYIN)
    assert detail["hit"] is False
    assert "证据行不在保存的行里" in detail["reason"]


def test_factor_requires_exact_factor_code():
    alternatives = [{"factor": "ec_ad_spend", "why": "花费因子下降"}]
    assert v21.match_mechanism(cause({"factor_metric_code": "ec_ad_spend"}), {}, alternatives)["hit"] is True
    assert v21.match_mechanism(cause({"factor_metric_code": "ec_ad_spend_extra"}), {}, alternatives)["hit"] is False
    assert v21.match_mechanism(cause({"member": "ec_ad_spend"}), {}, alternatives)["hit"] is False


@pytest.mark.parametrize("change", [{"status": "unverified"}, {"direction": "up"}, {"type": "calendar"}])
def test_primary_preserves_verification_direction_and_type_requirements(change):
    first = {**cause(), **change}
    run = run_record([dataset()], causes=[first])
    assert v21.score(mechanism_question(), run)["primary_hit"] is False


def test_offsetting_only_checks_first_two_and_uses_matching_direction():
    q = mechanism_question()
    q["expect"]["conclusion"] = ["offsetting"]
    first, second = cause(direction="up"), cause()
    run = run_record([dataset()], causes=[first, second], conclusion="offsetting")
    assert v21.score(q, run)["primary_hit"] is True
    run["report"]["causes"] = [first, first, second]
    assert v21.score(q, run)["primary_hit"] is False


def test_calendar_primary_without_mechanism_keeps_v1_entity_free_rule():
    q = mechanism_question([])
    q["expect"]["primary"]["entity"] = []
    q.pop("mechanism")
    run = run_record(causes=[cause({"member": "无须匹配"})])
    assert v21.score(q, run)["primary_hit"] is True


@pytest.mark.parametrize("keyword,member", [("抖音", "douyin"), ("douyin", "抖音")])
def test_forbidden_keyword_expands_channel_aliases(keyword, member):
    q = mechanism_question()
    q["expect"]["forbidden"] = [keyword]
    run = run_record([dataset()], causes=[cause({"dimension": "channel", "member": member})])
    result = v21.score(q, run)
    assert result["forbidden"] == [keyword]
    assert result["passed"] is False


@pytest.mark.parametrize("keyword,member", [("西南", "SW"), ("SW", "西南")])
def test_required_keyword_expands_region_aliases_and_requires_verified_causes(keyword, member):
    q = mechanism_question()
    q["expect"]["required_in_causes"] = [keyword]
    first = cause({"member": member})
    run = run_record(causes=[first])
    assert v21.score(q, run)["required_in_causes"] is True
    first["status"] = "unverified"
    assert v21.score(q, run)["required_in_causes"] is False


@pytest.mark.parametrize("condition", [
    "order_date >= '2026-09-01' AND order_date <= '2026-09-30'",
    "order_date > '2026-08-31' AND order_date < '2026-10-01'",
    "order_date BETWEEN '2026-09-01' AND '2026-09-30'",
    "order_date >= DATE '2026-09-01' AND order_date < DATE '2026-10-01'",
    "order_date >= CAST('2026-09-01' AS DATE) AND order_date <= CAST('2026-09-30' AS DATE)",
])
def test_explore_period_predicates_accept_strings_date_and_cast(condition):
    sql = f"SELECT SUM(order_cnt) AS orders FROM ec_dw.dwd_order WHERE {condition} " \
          "AND region_code = 'EC' AND source_channel IN ('douyin')"
    result = key_result(sql, columns=["orders"], rows=[[100]])
    assert result["hit"] is True, result


@pytest.mark.parametrize("expression", ["DATE_TRUNC('month', order_date)",
                                        "date_trunc('MONTH', order_date)",
                                        "CAST(DATE_TRUNC('month', order_date) AS DATE)"])
def test_month_grouped_row_binds_complete_month(expression):
    sql = f"SELECT {expression} AS month, region_code, source_channel, SUM(order_cnt) AS orders " \
          f"FROM ec_dw.dwd_order GROUP BY {expression}, region_code, source_channel"
    result = key_result(sql, columns=["month", "region_code", "source_channel", "orders"],
                        rows=[["2026-09-01", "EC", "douyin", 100]])
    assert result["hit"] is True, result


def test_daily_grouped_row_cannot_stand_for_month():
    sql = "SELECT order_date, region_code, source_channel, SUM(order_cnt) AS orders " \
          "FROM ec_dw.dwd_order GROUP BY order_date, region_code, source_channel"
    result = key_result(sql, columns=["order_date", "region_code", "source_channel", "orders"],
                        rows=[["2026-09-01", "EC", "douyin", 100]])
    assert result["hit"] is False
    assert "单日" in result["reason"]


def test_equal_date_predicate_is_single_day():
    sql = "SELECT SUM(order_cnt) AS orders FROM ec_dw.dwd_order WHERE order_date = DATE '2026-09-01' " \
          "AND region_code = 'EC' AND source_channel = 'douyin'"
    result = key_result(sql, columns=["orders"], rows=[[100]])
    assert result["hit"] is False
    assert "单日" in result["reason"]


@pytest.mark.parametrize("condition", ["order_date < '2026-10-01'", "order_date >= '2026-09-01'", "1=1"])
def test_unbounded_period_is_uncertain(condition):
    sql = f"SELECT SUM(order_cnt) AS orders FROM ec_dw.dwd_order WHERE {condition} " \
          "AND region_code = 'EC' AND source_channel = 'douyin'"
    result = key_result(sql, columns=["orders"], rows=[[100]])
    assert result["hit"] is False
    assert "窗口不确定" in result["reason"]


def test_constant_date_between_does_not_prove_dataset_window():
    sql = "SELECT SUM(order_cnt) AS orders FROM ec_dw.dwd_order WHERE " \
          "DATE '2026-09-15' BETWEEN DATE '2026-09-01' AND DATE '2026-09-30' " \
          "AND region_code = 'EC' AND source_channel = 'douyin'"
    result = key_result(sql, columns=["orders"], rows=[[100]])
    assert result["hit"] is False
    assert "窗口不确定" in result["reason"]


def test_selected_case_column_intersects_where_period():
    sql = "SELECT SUM(CASE WHEN order_date >= DATE '2026-09-01' THEN order_cnt ELSE 0 END) AS orders " \
          "FROM ec_dw.dwd_order WHERE order_date >= DATE '2026-08-01' " \
          "AND order_date < CAST('2026-10-01' AS DATE) AND region_code = 'EC' AND source_channel = 'douyin'"
    result = key_result(sql, columns=["orders"], rows=[[100]])
    assert result["hit"] is True, result


def test_case_in_other_column_does_not_supply_selected_column_window():
    sql = "SELECT SUM(order_cnt) AS orders, SUM(CASE WHEN order_date >= '2026-09-01' " \
          "AND order_date <= '2026-09-30' THEN order_cnt ELSE 0 END) AS other " \
          "FROM ec_dw.dwd_order WHERE region_code = 'EC' AND source_channel = 'douyin'"
    result = key_result(sql, columns=["orders", "other"], rows=[[100, 100]])
    assert result["hit"] is False
    assert "窗口不确定" in result["reason"]


@pytest.mark.parametrize("compare_start,compare_end,hit", [
    ("2026-08-01", "2026-08-31", True),
    ("2026-07-01", "2026-07-31", False),
])
def test_direct_sql_change_requires_both_case_periods(compare_start, compare_end, hit):
    sql = "SELECT SUM(CASE WHEN order_date BETWEEN '2026-09-01' AND '2026-09-30' " \
          "THEN order_cnt ELSE 0 END) - " \
          f"SUM(CASE WHEN order_date BETWEEN '{compare_start}' AND '{compare_end}' " \
          "THEN order_cnt ELSE 0 END) AS orders FROM ec_dw.dwd_order " \
          "WHERE region_code = 'EC' AND source_channel = 'douyin'"
    result = key_result(sql, columns=["orders"], rows=[[-20]], raw=-20)
    assert result["hit"] is hit, result


@pytest.mark.parametrize("expression", [
    "SUM(CASE WHEN order_date >= '2026-09-01' THEN order_cnt ELSE order_cnt END)",
    "SUM(CASE WHEN order_date >= '2026-09-01' THEN order_cnt ELSE 0 END) + SUM(order_cnt)",
])
def test_other_value_branches_prevent_case_from_narrowing_wide_where(expression):
    sql = f"SELECT {expression} AS orders FROM ec_dw.dwd_order " \
          "WHERE order_date BETWEEN '2026-08-01' AND '2026-09-30' " \
          "AND region_code = 'EC' AND source_channel = 'douyin'"
    result = key_result(sql, columns=["orders"], rows=[[100]])
    assert result["hit"] is False


def test_explore_entity_requires_every_expected_member():
    sql = "SELECT SUM(order_cnt) AS orders FROM ec_dw.dwd_order WHERE " \
          "order_date BETWEEN '2026-09-01' AND '2026-09-30' AND source_channel = 'douyin'"
    result = key_result(sql, columns=["orders"], rows=[[100]])
    assert result["hit"] is False
    assert result.get("reason")


def test_multi_value_in_does_not_prove_one_entity():
    sql = "SELECT SUM(order_cnt) AS orders FROM ec_dw.dwd_order WHERE " \
          "order_date BETWEEN '2026-09-01' AND '2026-09-30' AND region_code = 'EC' " \
          "AND source_channel IN ('douyin', 'organic')"
    result = key_result(sql, columns=["orders"], rows=[[100]])
    assert result["hit"] is False


def test_extra_explore_filters_are_reported_and_allowed():
    sql = "SELECT SUM(order_cnt) AS orders FROM ec_dw.dwd_order WHERE " \
          "order_date BETWEEN '2026-09-01' AND '2026-09-30' AND region_code = 'EC' " \
          "AND source_channel = 'douyin' AND status = 'paid'"
    result = key_result(sql, columns=["orders"], rows=[[100]])
    assert result["hit"] is True, result
    assert "paid" in str(result["binding"])


def test_explore_marker_also_enables_metric_binding():
    sql = "SELECT SUM(order_cnt) AS orders FROM ec_dw.dwd_order WHERE " \
          "order_date BETWEEN '2026-09-01' AND '2026-09-30' AND region_code = 'EC' AND source_channel = 'douyin'"
    run = run_record([dataset(kind="governed", sql=sql, columns=["orders"], rows=[[100]])],
                     segment=value_segment(explore=True))
    result = v21.key_value(metric_question(), run["report"], run)
    assert result["hit"] is True, result


@pytest.mark.parametrize("raw,absolute,tolerance,hit", [(101, False, 0.01, True),
                                                       (102, False, 0.01, False),
                                                       (-100, True, 0, True),
                                                       (-100, False, 0, False)])
def test_explore_numbers_keep_v2_tolerance_and_abs(raw, absolute, tolerance, hit):
    sql = "SELECT SUM(order_cnt) AS orders FROM ec_dw.dwd_order WHERE " \
          "order_date BETWEEN '2026-09-01' AND '2026-09-30' AND region_code = 'EC' AND source_channel = 'douyin'"
    result = key_result(sql, columns=["orders"], rows=[[raw]], raw=raw,
                        q=metric_question(absolute=absolute, tolerance=tolerance))
    assert result["hit"] is hit, result


def contribution_run(*, previous_start="2026-08-01", previous_end="2026-08-31", field="change", raw=-20):
    sql = "SELECT source_channel, region_code, " \
          f"SUM(CASE WHEN order_date BETWEEN '{previous_start}' AND '{previous_end}' " \
          "THEN order_cnt ELSE 0 END) AS previous_orders, " \
          "SUM(CASE WHEN order_date BETWEEN '2026-09-01' AND '2026-09-30' " \
          "THEN order_cnt ELSE 0 END) AS current_orders FROM ec_dw.dwd_order " \
          "GROUP BY source_channel, region_code"
    source = dataset(sql=sql, columns=["source_channel", "region_code", "previous_orders", "current_orders"],
                     rows=[["douyin", "EC", 120, 100]])
    output = dataset("D2", kind="calc", columns=["source_channel", "region_code", "previous", "current", "change"],
                     rows=[["douyin", "EC", 120, 100, -20]], inputs=["D1"])
    step = {"dataset": "D2", "tool": "contribution", "args": {
        "dataset": "D1", "keys": ["source_channel", "region_code"],
        "current": "current_orders", "previous": "previous_orders"}}
    return run_record([source, output], steps=[step], segment=value_segment(f"D2.{field}[#1]", raw))


@pytest.mark.parametrize("field,raw", [("change", -20), ("current", 100), ("previous", 120)])
def test_contribution_over_explore_uses_input_columns_and_keys(field, raw):
    run = contribution_run(field=field, raw=raw)
    result = v21.key_value(metric_question(), run["report"], run)
    assert result["hit"] is True, result


def test_contribution_change_rejects_wrong_previous_window():
    run = contribution_run(previous_start="2026-07-01", previous_end="2026-07-31")
    result = v21.key_value(metric_question(), run["report"], run)
    assert result["hit"] is False
    assert result.get("reason")


def test_contribution_can_aggregate_multiple_input_rows_with_same_keys():
    run = contribution_run()
    source = run["datasets"][0]
    source["sql"] = source["sql"].replace("SELECT source_channel, region_code, ",
                                          "SELECT source_channel, region_code, platform, ")
    source["sql"] += ", platform"
    source["columns"] = ["source_channel", "region_code", "platform", "previous_orders", "current_orders"]
    source["rows"] = [["douyin", "EC", "app", 70, 50], ["douyin", "EC", "web", 50, 50]]
    source["row_count"] = 2
    result = v21.key_value(metric_question(), run["report"], run)
    assert result["hit"] is True, result


@pytest.mark.parametrize("daily_values,hit", [
    ([("2026-09-01", 40), ("2026-09-02", 60)], True),
    ([("2026-09-01", 100)], False),
])
def test_contribution_merges_daily_input_windows_without_filling_missing_dates(daily_values, hit):
    run = contribution_run(field="current", raw=100)
    source = run["datasets"][0]
    source["sql"] = "SELECT order_date, source_channel, region_code, " \
                    "SUM(order_cnt) AS previous_orders, SUM(order_cnt) AS current_orders " \
                    "FROM ec_dw.dwd_order WHERE order_date BETWEEN '2026-09-01' AND '2026-09-02' " \
                    "GROUP BY order_date, source_channel, region_code"
    source["columns"] = ["order_date", "source_channel", "region_code", "previous_orders", "current_orders"]
    source["rows"] = [[day, "douyin", "EC", 120, value] for day, value in daily_values]
    source["row_count"] = len(source["rows"])
    q = metric_question()
    q["window"]["end"] = "2026-09-02"
    run["window"] = dict(q["window"])
    result = v21.key_value(q, run["report"], run)
    assert result["hit"] is hit, result


def test_contribution_mechanism_preserves_keys_and_adds_source_asset():
    run = contribution_run()
    first = cause({"evidence_id": "D2#1"}, evidence=["D2"])
    assert v21.match_mechanism(first, run, DOUYIN)["hit"] is True


def test_derive_numbers_still_cannot_bind_even_with_explore_flag():
    _, run = derived_run(["douyin"])
    run["report"]["fields"] = {"summary": {"segments": [value_segment("D2.value[#2]", 100, explore=True)]}}
    result = v21.key_value(metric_question(), run["report"], run)
    assert result["hit"] is False
    assert "derive" in result["reason"] or "派生" in result["reason"]


def test_legacy_explore_table_column_binding_keeps_v2_behavior():
    q = metric_question()
    q["v2"]["key_bindings"] = [{"explore": {"table": "ec_marketing.campaign", "column": "discount_rate"},
                                 "value": 0.85}]
    result = key_result("SELECT discount_rate FROM ec_marketing.campaign", q=q, path="D1.discount_rate[#1]",
                        raw=0.85, columns=["discount_rate"], rows=[[0.85]])
    assert result["hit"] is True, result


def complete_question_and_run(*, end="2026-09-30"):
    q = mechanism_question()
    numeric = metric_question()
    q["expect"].update(numeric["expect"])
    q["v2"] = numeric["v2"]
    sql = "SELECT source_channel, SUM(order_cnt) AS orders FROM ec_dw.dwd_order WHERE " \
          f"order_date BETWEEN '2026-09-01' AND '{end}' " \
          "AND region_code = 'EC' AND source_channel = 'douyin' GROUP BY source_channel"
    run = run_record([dataset(sql=sql)], causes=[cause()], segment=value_segment())
    return q, run


@pytest.mark.parametrize("end,hit", [("2026-09-30", True), ("2026-09-01", False)])
def test_score_applies_explore_numeric_result_to_overall_passed(end, hit):
    q, run = complete_question_and_run(end=end)
    result = v21.score(q, run)
    assert result["primary_hit"] is True
    assert result["key_value_hit"] is hit
    assert result["passed"] is hit


def test_unfinished_run_cannot_hit_primary_or_numeric_value():
    q, run = complete_question_and_run()
    run["status"] = "failed"
    result = v21.score(q, run)
    assert result["primary_hit"] is False
    assert result["primary_detail"]["hit"] is False
    assert result["key_value_hit"] is False
    assert result["passed"] is False


def test_alias_expansion_does_not_mutate_question_or_run():
    q, run = complete_question_and_run()
    q["expect"].update(forbidden=["抖音"], required_in_causes=["西南"])
    run["report"]["causes"] = [cause({"member": "douyin"}), cause({"member": "SW"})]
    original_q, original_run = deepcopy(q), deepcopy(run)
    result = v21.score(q, run)
    assert result["forbidden"] == ["抖音"]
    assert result["required_in_causes"] is True
    assert q == original_q
    assert run == original_run


def test_load_questions_rejects_empty_truth_without_implicit_file_read(monkeypatch):
    def forbidden_load_truth():
        raise AssertionError("空真值不得隐式读取文件")

    monkeypatch.setattr(v21.v1, "load_truth", forbidden_load_truth)
    with pytest.raises(ValueError):
        v21.load_questions(truth={})


def audit_row(key, hit, source="e1:handwritten.json"):
    return {"audit_key": key, "source": source, "v2_1": {"primary_hit": hit}}

