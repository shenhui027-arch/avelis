"""截断规则的离线回归：仅使用手写记录，不运行重评入口或读取演示数据。"""

from copy import deepcopy

import pytest

from tests.eval import scoring_v2_1 as v21
from tests.eval import scoring_v2_1_2 as v212
from tests.eval import scoring_v2_1_3 as v213
from tests.unit.test_eval_scoring_v2_1_2 import (
    contribution_run,
    dataset,
    explain_run,
    factor_run,
    governed,
    metric_question,
    run_record,
)


def key_case(branch):
    question = metric_question()
    if branch == "governed":
        source, step = governed()
        run = run_record([source], [step], "D1.订单数[#1]", 100)
    elif branch == "bucket":
        source, step = governed(grain="month", columns=["period", "订单数"], rows=[["2026-09", 100]])
        run = run_record([source], [step], "D1.订单数[#1]", 100)
    elif branch == "explain":
        run = explain_run(
            path="D2.member_change[#1]", filters=[], rows=[["region×channel", "华东 / 抖音", 120, 100, -20, -20]]
        )
    elif branch == "summary":
        run = explain_run()
    elif branch == "factor":
        question = metric_question(metric="ec_ad_spend")
        run = factor_run()
    elif branch == "calendar":
        source, step = governed()
        result = dataset("D2", kind="calc", inputs=["D1"], columns=["period", "value"], rows=[])
        calendar = {
            "tool": "calendar_adjust",
            "dataset": "D2",
            "args": {"dataset": "D1", "current": "订单数", "previous": "订单数·对比期"},
        }
        question["v2"]["key_bindings"][0].update(truth=None, value=100, calc="calendar.day_count_effect")
        run = run_record([source, result], [step, calendar], "D2.summary.day_count_effect", 100)
    elif branch == "contribution":
        run = contribution_run([["douyin", "EC", "app", 120, 100]])
    elif branch == "governed_contribution":
        source, step = governed(columns=["channel", "订单数", "订单数·对比期"], rows=[["抖音", 100, 120]])
        result = dataset(
            "D2",
            kind="calc",
            inputs=["D1"],
            columns=["channel", "previous", "current", "change"],
            rows=[["抖音", 120, 100, -20]],
        )
        contribution = {
            "tool": "contribution",
            "dataset": "D2",
            "args": {"dataset": "D1", "keys": ["channel"], "current": "订单数", "previous": "订单数·对比期"},
        }
        run = run_record([source, result], [step, contribution], "D2.change[#1]", -20)
    else:
        assert branch == "explore"
        sql = (
            "SELECT SUM(order_cnt) AS orders FROM ec_dw.dwd_order WHERE "
            "order_date BETWEEN '2026-09-01' AND '2026-09-30' "
            "AND region_code = 'EC' AND source_channel = 'douyin'"
        )
        source = dataset(kind="explore", columns=["orders"], rows=[[100]], sql=sql)
        run = run_record([source], [], "D1.orders[#1]", 100)
    return question, run


BRANCHES = [
    "governed",
    "bucket",
    "explain",
    "summary",
    "factor",
    "calendar",
    "contribution",
    "governed_contribution",
    "explore",
]


def truncate(run, ref):
    ds = next(ds for ds in run["datasets"] if ds["ref"] == ref)
    ds["row_count"] = len(ds["rows"]) + 1
    return f"数据集 {ref} 只保存了前 {len(ds['rows'])} 行（共 {ds['row_count']} 行），不能确定"


def without_new_fields(value):
    if isinstance(value, dict):
        return {
            key: without_new_fields(item)
            for key, item in value.items()
            if key not in {"scorer", "uncertain_truncated", "uncertain_details"}
        }
    if isinstance(value, list):
        return [without_new_fields(item) for item in value]
    return value


@pytest.mark.parametrize("branch", BRANCHES)
def test_truncated_selected_dataset_blocks_every_key_binding_branch(branch):
    question, run = key_case(branch)
    assert v212.score(question, run)["key_value_hit"] is True
    ref = run["report"]["fields"]["summary"]["segments"][0]["path"].split(".", 1)[0]
    why = truncate(run, ref)
    original = deepcopy((question, run))
    result = v213.score(question, run)
    detail = result["key_value_detail"]
    assert result["key_value_hit"] is result["passed"] is False
    assert detail["uncertain_truncated"] == 1
    assert why in detail["reason"] and why in detail["uncertain_details"][0]
    assert result["primary_detail"]["uncertain_truncated"] == 0
    assert (question, run) == original


@pytest.mark.parametrize("branch", BRANCHES)
def test_complete_datasets_preserve_v212_scores(branch):
    question, run = key_case(branch)
    old, new = v212.score(question, run), v213.score(question, run)
    assert new["scorer"] == "v2.1.3"
    assert without_new_fields(new) == without_new_fields(old)
    assert new["key_value_detail"]["uncertain_truncated"] == 0
    assert new["key_value_detail"]["uncertain_details"] == []


@pytest.mark.parametrize("branch", ["contribution", "governed_contribution"])
def test_contribution_checks_input_even_when_output_is_complete(branch):
    question, run = key_case(branch)
    why = truncate(run, "D1")
    assert v212.score(question, run)["key_value_hit"] is True
    detail = v213.score(question, run)["key_value_detail"]
    assert detail["hit"] is False and detail["uncertain_truncated"] == 1
    assert why in detail["reason"]


def test_contribution_checks_nested_input_before_expansion():
    question, run = key_case("contribution")
    result = dataset(
        "D3",
        kind="calc",
        inputs=["D2"],
        columns=["source_channel", "previous", "current", "change"],
        rows=[["douyin", 120, 100, -20]],
    )
    run["datasets"].append(result)
    run["steps"].append(
        {
            "tool": "contribution",
            "dataset": "D3",
            "args": {"dataset": "D2", "keys": ["source_channel"], "previous": "previous", "current": "current"},
        }
    )
    run["report"]["fields"]["summary"]["segments"][0]["path"] = "D3.change[#1]"
    assert v212.score(question, run)["key_value_hit"] is True
    why = truncate(run, "D1")
    detail = v213.score(question, run)["key_value_detail"]
    assert detail["hit"] is False and why in detail["reason"]
    assert detail["uncertain_truncated"] == 1


@pytest.mark.parametrize("branch", ["summary", "factor", "calendar"])
def test_metadata_only_inputs_are_not_blocked(branch):
    question, run = key_case(branch)
    truncate(run, "D1")
    assert without_new_fields(v213.score(question, run)) == without_new_fields(v212.score(question, run))
    assert v213.score(question, run)["key_value_hit"] is True


def mechanism_case(expansion=None):
    source = dataset(
        kind="explore",
        columns=["source_channel", "orders"],
        rows=[["douyin", 100]],
        sql="SELECT * FROM ec_dw.dwd_order",
    )
    run = run_record([source], [], "D1.orders[#1]", 100)
    cause = run["report"]["causes"][0]
    cause.update(entity={"evidence_id": "D1#1"}, evidence=["D1"])
    question = metric_question()
    question["expect"].pop("key_value")
    question["expect"]["primary"]["entity"] = ["抖音"]
    question["mechanism"] = [{"asset": "ec_trade.orders", "members": ["抖音"]}]
    if expansion:
        output = dataset("D2", kind="calc", inputs=["D1"], columns=["source_channel", "value"], rows=[["douyin", 100]])
        run["datasets"].append(output)
        args = {"values": [{"dataset": "D1", "match": {}}]} if expansion == "derive" else {"dataset": "D1"}
        run["steps"].append({"tool": expansion, "dataset": "D2", "args": args})
        cause.update(entity={"evidence_id": "D2#1"}, evidence=["D2"])
    return question, run


@pytest.mark.parametrize(
    "expansion,ref", [(None, "D1"), ("derive", "D1"), ("derive", "D2"), ("contribution", "D1"), ("contribution", "D2")]
)
def test_mechanism_blocks_evidence_and_expansion_input_datasets(expansion, ref):
    question, run = mechanism_case(expansion)
    assert v212.score(question, run)["primary_hit"] is True
    why = truncate(run, ref)
    result = v213.score(question, run)
    detail = result["primary_detail"]
    assert result["primary_hit"] is result["passed"] is False
    assert detail == result["mechanism"]
    assert detail["uncertain_truncated"] == 1 and why in detail["reason"]
    assert why in detail["uncertain_details"][0]
    assert result["key_value_detail"]["uncertain_truncated"] == 0


@pytest.mark.parametrize("change", [None, "status", "type", "direction", "offsetting", "no_options"])
@pytest.mark.parametrize("expansion", [None, "derive", "contribution"])
def test_complete_mechanism_keeps_all_other_v212_gates(change, expansion):
    question, run = mechanism_case(expansion)
    cause = run["report"]["causes"][0]
    if change in {"status", "type", "direction"}:
        cause[change] = {"status": "unverified", "type": "calendar", "direction": "up"}[change]
    elif change == "offsetting":
        run["report"]["conclusion"] = "offsetting"
        question["expect"]["conclusion"] = ["offsetting"]
        run["report"]["causes"].insert(0, {**cause, "direction": "up"})
    elif change == "no_options":
        question.pop("mechanism")
    assert without_new_fields(v213.score(question, run)) == without_new_fields(v212.score(question, run))


def test_sql_only_source_and_key_binding_are_unaffected_by_truncation():
    question, run = key_case("explore")
    truncate(run, "D1")
    question["v2"]["key_bindings"] = [{"value": 100, "explore": {"table": "ec_dw.dwd_order", "column": "orders"}}]
    assert without_new_fields(v213.score(question, run)) == without_new_fields(v212.score(question, run))
    assert v213.score(question, run)["key_value_hit"] is True
    assert v21.source_tables(run, ["D1"]) == {"ec_dw.dwd_order"}


@pytest.mark.parametrize("kind", ["member", "factor"])
def test_mechanism_without_reading_rows_is_unaffected(kind):
    question, run = mechanism_case()
    truncate(run, "D1")
    cause = run["report"]["causes"][0]
    if kind == "member":
        cause["entity"].update(member="抖音", dimension="channel")
    else:
        cause["entity"]["factor_metric_code"] = "ec_ad_spend"
        question["mechanism"] = [{"factor": "ec_ad_spend"}]
    assert without_new_fields(v213.score(question, run)) == without_new_fields(v212.score(question, run))
    assert v213.score(question, run)["primary_hit"] is True
    assert v213.score(question, run)["primary_detail"]["uncertain_truncated"] == 0


def test_derive_key_path_checks_input_but_does_not_enable_formula_binding():
    question, run = key_case("governed")
    run["datasets"].append(dataset("D2", kind="calc", title="派生：示例", columns=["value"], rows=[[100]]))
    run["steps"].append({"tool": "derive", "dataset": "D2", "args": {"values": [{"dataset": "D1"}]}})
    run["report"]["fields"]["summary"]["segments"][0]["path"] = "D2.value[#1]"
    assert without_new_fields(v213.score(question, run)) == without_new_fields(v212.score(question, run))
    why = truncate(run, "D1")
    detail = v213.score(question, run)["key_value_detail"]
    assert detail["hit"] is False and detail["uncertain_truncated"] == 1
    assert why in detail["reason"]


def test_counts_unique_paths_caps_details_and_keeps_later_complete_candidates():
    question, run = key_case("governed")
    segments = run["report"]["fields"]["summary"]["segments"]
    for i in range(2, 8):
        ds = deepcopy(run["datasets"][0])
        ds["ref"] = f"D{i}"
        run["datasets"].append(ds)
        run["steps"].append({**deepcopy(run["steps"][0]), "dataset": f"D{i}"})
        segments.append({**segments[0], "path": f"D{i}.订单数[#1]"})
    for i in range(1, 8):
        truncate(run, f"D{i}")
    segments.append(deepcopy(segments[0]))
    question["v2"]["key_bindings"] *= 2
    detail = v213.score(question, run)["key_value_detail"]
    assert detail["hit"] is False and detail["uncertain_truncated"] == 7
    assert len(detail["uncertain_details"]) == 5
    run["datasets"][-1]["row_count"] = 1
    result = v213.score(question, run)
    assert result["key_value_hit"] is result["passed"] is True
    assert result["key_value_detail"]["uncertain_truncated"] == 6


def test_nonmatching_numbers_manual_and_unrelated_datasets_do_not_count():
    question, run = key_case("governed")
    run["datasets"].append(dataset("D9", rows=[], columns=["x"]))
    truncate(run, "D9")
    assert v213.score(question, run)["key_value_hit"] is True
    truncate(run, "D1")
    run["report"]["fields"]["summary"]["segments"][0]["raw"] = 999
    assert v213.score(question, run)["key_value_detail"]["uncertain_truncated"] == 0
    question["v2"]["key_bindings"][0]["manual"] = True
    assert v213.score(question, run)["key_value_hit"] is None
    assert v213.score(question, run)["key_value_detail"]["uncertain_truncated"] == 0


@pytest.mark.parametrize(
    "status,error,lost",
    [
        ("failed", "普通失败", None),
        ("failed", "权限失效", "权限失效"),
        ("failed", "APITimeoutError: timed out", "权限失效"),
    ],
)
def test_incomplete_permission_results_keep_v212_behavior(status, error, lost):
    question = metric_question()
    question.update(category="adversarial")
    question["expect"]["denied_ok"] = True
    question["v2"]["unauthorized"] = {"metric_prefix": "ec_", "table_db_prefix": "ec_"}
    run = {
        "status": status,
        "error": error,
        "datasets": [],
        "steps": [],
        "report": {"permission": {"lost": lost, "denials": []}},
    }
    assert without_new_fields(v213.score(question, run)) == without_new_fields(v212.score(question, run))
