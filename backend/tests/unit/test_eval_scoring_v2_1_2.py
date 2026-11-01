"""评分器 v2.1.2 的六处回归：只用手写最小运行记录，不读取演示数据。"""

from copy import deepcopy
from decimal import Decimal

import pytest

from tests.eval import scoring_v2_1 as v21
from tests.eval import scoring_v2_1_1 as v211
from tests.eval import scoring_v2_1_2 as v212

WINDOW = {"start": "2026-09-01", "end": "2026-09-30",
          "compare_start": "2026-08-01", "compare_end": "2026-08-31"}
ENTITY = {"region": "华东", "channel": "抖音"}
FILTERS = [{"dimension": name, "values": [value]} for name, value in ENTITY.items()]


def dataset(ref="D1", *, kind="governed", title="手写数据", columns=None, rows=None, inputs=None, sql=None):
    rows = rows if rows is not None else []
    return {"ref": ref, "kind": kind, "title": title, "columns": columns or [], "rows": rows,
            "row_count": len(rows), "inputs": inputs or [], "sql": sql or "SELECT 1"}


def metric_question(*, metric="ec_ad_orders", entity=None, window=None):
    return {"id": "handwritten", "category": "dimension", "window": dict(window or WINDOW),
            "expect": {"conclusion": ["concentrated"],
                       "primary": {"types": ["member"], "direction": "down", "entity": []},
                       "key_value": {"value": 100, "tolerance": 0}},
            "v2": {"key_bindings": [{"metric": metric, "entity": dict(ENTITY if entity is None else entity),
                                     "truth": {"current": Decimal(100), "compare": Decimal(120),
                                               "change": Decimal(-20)}}]}}


def run_record(datasets, steps, path, raw, *, window=None):
    return {"status": "succeeded", "window": dict(window or WINDOW), "datasets": datasets, "steps": steps,
            "verification": {}, "report": {"conclusion": "concentrated",
                "causes": [{"type": "member", "direction": "down", "status": "verified", "entity": {}}],
                "fields": {"summary": {"segments": [{"kind": "value", "path": path, "raw": raw}]}}}}


def key_result(question, run):
    return v212.key_value(question, run["report"], run)


def governed(*, metric="ec_ad_orders", name="订单数", filters=None, group=None, grain=None,
             window=None, compare="custom", columns=None, rows=None):
    window = dict(window or WINDOW)
    args = {"metric_codes": [metric], "filters": deepcopy(FILTERS if filters is None else filters),
            "group_by": group or [], "time_grain": grain,
            "start_date": window["start"], "end_date": window["end"], "compare": compare,
            "compare_start": window["compare_start"], "compare_end": window["compare_end"]}
    title = f"{name} {window['start']}~{window['end']}"
    if group:
        title += f" 按{'、'.join(group)}"
    if grain:
        title += f" 按{'月' if grain == 'month' else '日'}"
    if compare:
        label = {"previous_period": "上一期", "previous_year": "去年同期"}.get(compare)
        title += f" 对比{label or window['compare_start'] + '~' + window['compare_end']}"
    source = dataset(title=title, columns=columns or [name], rows=rows or [[100]])
    step = {"kind": "tool", "tool": "query_metric", "dataset": "D1", "args": args}
    return source, step


def contribution_run(rows, *, field="change", raw=-20, keys=None, output=None):
    sql = "SELECT source_channel, region_code, platform, " \
          "SUM(CASE WHEN order_date BETWEEN '2026-08-01' AND '2026-08-31' " \
          "THEN order_cnt ELSE 0 END) AS previous_orders, " \
          "SUM(CASE WHEN order_date BETWEEN '2026-09-01' AND '2026-09-30' " \
          "THEN order_cnt ELSE 0 END) AS current_orders FROM ec_dw.dwd_order " \
          "GROUP BY source_channel, region_code, platform"
    source = dataset(kind="explore", sql=sql,
                     columns=["source_channel", "region_code", "platform", "previous_orders", "current_orders"],
                     rows=rows)
    keys = ["source_channel"] if keys is None else keys
    output = output or ["douyin", 120, 100, -20]
    result = dataset("D2", kind="calc", inputs=["D1"], columns=[*keys, "previous", "current", "change"],
                     rows=[output])
    step = {"tool": "contribution", "dataset": "D2", "args": {
        "dataset": "D1", "keys": keys, "previous": "previous_orders", "current": "current_orders"}}
    return run_record([source, result], [step], f"D2.{field}[#1]", raw)


def test_contribution_rejects_thread_4430232921_cross_region_same_change():
    # 两区域合计的变化恰好等于华东变化，仍不能证明华东的数值。
    run = contribution_run([["douyin", "EC", "app", 120, 100], ["douyin", "SW", "app", 50, 50]],
                           output=["douyin", 170, 150, -20])
    question = metric_question()
    assert v21.key_value(question, run["report"], run)["hit"] is True
    result = key_result(question, run)
    assert result["hit"] is False
    assert "contribution 聚合了不属于该实体的行" in result["reason"]


@pytest.mark.parametrize("field,raw", [("current", 100), ("previous", 120), ("change", -20)])
def test_contribution_accepts_every_input_row_belonging_to_expected_entity(field, raw):
    # 聚合键不含区域，但两条来源逐行都属于华东抖音。
    run = contribution_run([["douyin", "EC", "app", 70, 50], ["douyin", "EC", "web", 50, 50]],
                           field=field, raw=raw)
    result = key_result(metric_question(), run)
    assert result["hit"] is True, result


@pytest.mark.parametrize("rows,hit", [
    ([["douyin", "EC", "app", 120, 100]], True),
    ([["douyin", "EC", "app", 70, 50], ["organic", "EC", "web", 50, 50]], False),
])
def test_contribution_requires_each_expected_value_in_every_selected_row(rows, hit):
    run = contribution_run(rows, keys=[], output=[120, 100, -20])
    assert key_result(metric_question(), run)["hit"] is hit


@pytest.mark.parametrize("region,hit", [("EC", True), ("SW", False)])
def test_non_contribution_explore_keeps_v21_entity_and_window_rules(region, hit):
    sql = "SELECT SUM(order_cnt) AS orders FROM ec_dw.dwd_order WHERE " \
          "order_date BETWEEN '2026-09-01' AND '2026-09-30' " \
          f"AND region_code = '{region}' AND source_channel = 'douyin'"
    source = dataset(kind="explore", columns=["orders"], rows=[[100]], sql=sql)
    run = run_record([source], [], "D1.orders[#1]", 100)
    assert key_result(metric_question(), run)["hit"] is hit


def explain_run(*, path="D2.summary.total.change", raw=-20, filters=None, rows=None):
    filters = deepcopy(FILTERS if filters is None else filters)
    source, query = governed(filters=filters)
    result = dataset("D2", kind="calc", inputs=["D1"],
                     columns=["dimension", "member", "previous", "current", "member_change", "contribution"],
                     rows=rows if rows is not None else [["city", "上海", 70, 50, -20, -20],
                                                        ["city", "杭州", 50, 50, 0, 0]])
    step = {"tool": "explain_change", "dataset": "D2", "args": {
        "metric_code": "ec_ad_orders", "filters": filters,
        "start_date": WINDOW["start"], "end_date": WINDOW["end"],
        "compare_start": WINDOW["compare_start"], "compare_end": WINDOW["compare_end"]}}
    return run_record([source, result], [query, step], path, raw)


@pytest.mark.parametrize("field,raw,period,calc", [
    ("current", 100, "current", "level"), ("previous", 120, "compare", "level"),
    ("change", -20, "change", "delta"),
])
def test_explain_summary_total_binds_scope_and_period_without_member_selection(field, raw, period, calc):
    run = explain_run(path=f"D2.summary.total.{field}", raw=raw)
    result = key_result(metric_question(), run)
    assert result["hit"] is True, result
    assert result["binding"]["entity"] == ENTITY
    assert result["binding"]["period"] == period
    assert result["binding"]["calc"] == calc


@pytest.mark.parametrize("change", ["filters", "period", "wrong_column"])
def test_explain_summary_total_rejects_wrong_scope_period_and_numeric_role(change):
    run = explain_run()
    if change == "filters":
        run["steps"][-1]["args"]["filters"] = [{"dimension": "region", "values": ["华南"]}]
    elif change == "period":
        run["steps"][-1]["args"]["compare_start"] = "2026-07-01"
        run["steps"][-1]["args"]["compare_end"] = "2026-07-31"
    else:
        run["report"]["fields"]["summary"]["segments"][0]["path"] = "D2.summary.total.current"
    result = key_result(metric_question(), run)
    assert result["hit"] is False
    assert result.get("reason")


@pytest.mark.parametrize("mode,suffix,compare_start,compare_end", [
    ("previous_period", "上一期", "2026-08-02", "2026-08-31"),
    ("previous_year", "去年同期", "2025-09-01", "2025-09-30"),
    ("custom", "对比期", "2026-08-01", "2026-08-31"),
])
@pytest.mark.parametrize("correct_window", [True, False])
def test_governed_compare_suffixes_bind_shifted_window(mode, suffix, compare_start, compare_end, correct_window):
    window = {**WINDOW, "compare_start": compare_start, "compare_end": compare_end}
    source, step = governed(window=window, compare=mode, columns=[f"订单数·{suffix}"], rows=[[120]])
    run = run_record([source], [step], f"D1.订单数·{suffix}[#1]", 120, window=window)
    question = metric_question(window=window)
    if not correct_window:
        question["window"]["compare_start"] = "2026-07-01"
        question["window"]["compare_end"] = "2026-07-31"
    result = key_result(question, run)
    assert result["hit"] is correct_window, result
    if correct_window:
        assert result["binding"]["period"] == "compare"


def factor_run(*, match=None, field="current", raw=100):
    source, query = governed(metric="ec_ad_spend", name="投放花费",
                             filters=[{"dimension": "channel", "values": ["抖音"]}], group=["region"],
                             columns=["region", "投放花费", "投放花费·对比期"], rows=[["华东", 100, 120]])
    result = dataset("D2", kind="calc", inputs=["D1"],
                     columns=["factor_metric_code", "previous", "current", "contribution"],
                     rows=[["ec_ad_spend", 120, 100, -20]])
    step = {"tool": "factor_decompose", "dataset": "D2", "args": {
        "dataset": "D1", "match": {"region": "华东"} if match is None else match}}
    return run_record([source, result], [query, step], f"D2.{field}[#1]", raw)


@pytest.mark.parametrize("field,raw", [("current", 100), ("previous", 120)])
def test_factor_decompose_adds_selected_match_to_entity(field, raw):
    result = key_result(metric_question(metric="ec_ad_spend"), factor_run(field=field, raw=raw))
    assert result["hit"] is True, result
    assert result["binding"]["entity"] == ENTITY


@pytest.mark.parametrize("match", [{}, {"region": "华南"}])
def test_factor_decompose_requires_the_expected_selected_match(match):
    result = key_result(metric_question(metric="ec_ad_spend"), factor_run(match=match))
    assert result["hit"] is False
    assert "实体" in result["reason"]


def test_factor_decompose_contribution_is_not_factor_level_value():
    run = factor_run(field="contribution", raw=-20)
    assert key_result(metric_question(metric="ec_ad_spend"), run)["hit"] is False


@pytest.mark.parametrize("grain,stamp,start,end,hit", [
    ("month", "2026-09", "2026-09-01", "2026-09-30", True),
    ("month", "2026-09-01", "2026-09-10", "2026-09-20", True),
    ("day", "2026-09-15", "2026-09-15", "2026-09-15", True),
    ("month", "2026-09", "2026-09-01", "2026-10-31", False),
    ("month", "2026-08", "2026-09-01", "2026-09-30", False),
    ("day", "2026-09-15", "2026-09-01", "2026-09-30", False),
])
def test_governed_bucket_intersection_must_equal_entire_question_period(grain, stamp, start, end, hit):
    window = {**WINDOW, "start": start, "end": end}
    source, step = governed(window=window, grain=grain, compare=None, group=["channel"],
                             filters=[{"dimension": "region", "values": ["华东"]}],
                             columns=["period", "channel", "订单数"], rows=[[stamp, "抖音", 100]])
    run = run_record([source], [step], "D1.订单数[#1]", 100, window=window)
    result = key_result(metric_question(window=window), run)
    assert result["hit"] is hit, result
    if hit:
        assert result["binding"]["entity"] == ENTITY
        assert result["binding"]["span"] == [(start, end)]


def test_governed_bucket_intersection_does_not_expand_a_short_query_to_question_period():
    query_window = {**WINDOW, "start": "2026-09-10", "end": "2026-09-20"}
    source, step = governed(window=query_window, grain="month", compare=None,
                             columns=["period", "订单数"], rows=[["2026-09", 100]])
    run = run_record([source], [step], "D1.订单数[#1]", 100)
    assert key_result(metric_question(), run)["hit"] is False


@pytest.mark.parametrize("compare_start,compare_end,hit", [
    ("2026-08-01", "2026-08-31", True),
    ("2026-08-15", "2026-09-15", False),
])
def test_governed_month_bucket_compare_column_requires_entire_compare_period(compare_start, compare_end, hit):
    window = {**WINDOW, "compare_start": compare_start, "compare_end": compare_end}
    source, step = governed(window=window, grain="month", columns=["period", "订单数·对比期"],
                             rows=[["2026-08", 120]])
    run = run_record([source], [step], "D1.订单数·对比期[#1]", 120, window=window)
    result = key_result(metric_question(window=window), run)
    assert result["hit"] is hit, result
    if hit:
        assert result["binding"]["period"] == "compare"
        assert result["binding"]["span"] == [(compare_start, compare_end)]


@pytest.mark.parametrize("grain,stamp,query_start,query_end,compare_start,compare_end,hit", [
    ("month", "2026-08", "2026-08-01", "2026-08-31", "2026-08-01", "2026-08-31", True),
    ("day", "2026-08-15", "2026-08-15", "2026-08-15", "2026-08-15", "2026-08-15", True),
    ("month", "2026-08", "2026-08-15", "2026-08-31", "2026-08-01", "2026-08-31", False),
    ("month", "2026-08", "2026-08-01", "2026-09-30", "2026-08-01", "2026-09-30", False),
])
def test_independently_queried_bucket_binds_only_the_entire_compare_period(
    grain, stamp, query_start, query_end, compare_start, compare_end, hit,
):
    # 产品按本期、对比期分别查询时间桶；对比期查询的普通指标列仍是题目的 compare。
    question_window = {**WINDOW, "compare_start": compare_start, "compare_end": compare_end}
    query_window = {**question_window, "start": query_start, "end": query_end}
    source, step = governed(window=query_window, grain=grain, compare=None,
                             columns=["period", "订单数"], rows=[[stamp, 120]])
    run = run_record([source], [step], "D1.订单数[#1]", 120, window=question_window)
    result = key_result(metric_question(window=question_window), run)
    assert result["hit"] is hit, result
    if hit:
        assert result["binding"]["period"] == "compare"
        assert result["binding"]["span"] == [(compare_start, compare_end)]


def test_non_bucket_query_for_compare_dates_keeps_the_original_column_period_rule():
    query_window = {**WINDOW, "start": WINDOW["compare_start"], "end": WINDOW["compare_end"]}
    source, step = governed(window=query_window, compare=None, rows=[[120]])
    run = run_record([source], [step], "D1.订单数[#1]", 120)
    assert key_result(metric_question(), run)["hit"] is False


def test_governed_day_bucket_requires_a_full_date_instead_of_a_month():
    window = {**WINDOW, "start": "2026-09-01", "end": "2026-09-01"}
    source, step = governed(window=window, grain="day", compare=None,
                             columns=["period", "订单数"], rows=[["2026-09", 100]])
    run = run_record([source], [step], "D1.订单数[#1]", 100, window=window)
    assert key_result(metric_question(window=window), run)["hit"] is False


@pytest.mark.parametrize("manual,unit,tolerance,absolute,raw,hit", [
    (True, None, 0, False, 100, None),
    (False, "元", 0, False, 100, False),
    (False, None, 0.01, False, 101, True),
    (False, None, 0.01, False, 102, False),
    (False, None, 0, True, -100, True),
    (False, None, 0, False, -100, False),
])
def test_key_value_preserves_manual_unit_tolerance_and_absolute_gates(manual, unit, tolerance, absolute, raw, hit):
    question = metric_question()
    question["expect"]["key_value"].update(tolerance=tolerance, abs=absolute)
    question["v2"]["key_bindings"][0]["manual"] = manual
    source, step = governed(rows=[[raw]])
    run = run_record([source], [step], "D1.订单数[#1]", raw)
    run["report"]["fields"]["summary"]["segments"][0]["unit"] = unit
    assert key_result(question, run)["hit"] is hit


@pytest.mark.parametrize("dimension,member,hit", [
    ("region×channel", "华东 / 抖音", True),
    ("channel×region", "抖音 / 华东", True),
    ("region×channel", "华南 / 抖音", False),
    ("region×channel", "华东/抖音", False),
    ("region×channel×city", "华东 / 抖音", False),
    ("region×channel", "华东 / 抖音 / 上海", False),
])
def test_explain_composite_member_splits_only_matching_dimension_and_member_counts(dimension, member, hit):
    run = explain_run(path="D2.member_change[#1]", filters=[], rows=[[dimension, member, 120, 100, -20, -20]])
    result = key_result(metric_question(), run)
    assert result["hit"] is hit, result
    if hit:
        assert result["binding"]["entity"] == ENTITY


def test_score_uses_new_key_binding_and_preserves_input_records():
    question = metric_question()
    run = explain_run()
    original = deepcopy((question, run))
    assert v211.score(question, run)["passed"] is False
    result = v212.score(question, run)
    assert result["scorer"] == "v2.1.2"
    assert result["passed"] is result["key_value_hit"] is True
    assert (question, run) == original


@pytest.mark.parametrize("lost,error,leaked,passed", [
    ("发起人权限失效", "发起人权限失效", False, True),
    (None, "普通运行失败", False, False),
    ("发起人权限失效", "APITimeoutError: timed out", False, False),
    ("发起人权限失效", "发起人权限失效", True, False),
])
def test_failed_permission_denial_keeps_v211_rule_even_with_primary_and_key_value(lost, error, leaked, passed):
    # 正确拒绝属于未完成运行，不应再被不存在的关键数值引用否决。
    question = metric_question()
    question["category"] = "adversarial"
    question["expect"]["denied_ok"] = True
    question["v2"]["unauthorized"] = {"metric_prefix": "ec_", "table_db_prefix": "ec_"}
    run = {"status": "failed", "error": error, "datasets": [],
           "report": {"permission": {"lost": lost, "denials": []}}, "steps": []}
    if leaked:
        run["steps"] = [{"seq": 1, "kind": "tool", "tool": "query_sql", "status": "ok",
                         "args": {"sql": "SELECT * FROM ec_org.store"}}]
    for scorer in (v211, v212):
        result = scorer.score(question, run)
        assert result["completed"] is False
        assert result["key_value_hit"] is False
        assert result["passed"] is result["policy_passed"] is passed


def audit_row(key, hit, source="e1:handwritten.json"):
    return {"audit_key": key, "source": source, "v2_1_2": {"primary_hit": hit}}

