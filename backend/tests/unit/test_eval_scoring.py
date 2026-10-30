"""评测集与判分（ADR 0009 第 5 点）：判分是纯函数，用手工构造的运行结果逐项核对。"""

from __future__ import annotations

from collections import Counter

from tests.eval.scoring import load_questions, markdown, score, summarize

DATA = load_questions()
Q = {q["id"]: q for q in DATA["questions"]}
D01, N01 = Q["d01"]["expect"]["conclusion"][0], Q["n01"]["expect"]["conclusion"][0]  # 由真值算出


def test_question_set_shape():
    holdout = [q for q in DATA["questions"] if q.get("holdout")]
    # 第一批留出题 h01–h06 看过失败细节后转入调优集；第二批 h07–h12 是当前的留出题
    assert len(holdout) == 6 and {q["id"] for q in holdout} == {f"h{i:02d}" for i in range(7, 13)}
    held = Counter(q["category"] for q in holdout)
    assert held["negative"] >= 1 and held["explore"] >= 1  # 至少各 1 道反例与探索题
    assert all("conclusion_from" in q["expect"] for q in holdout)  # 期望结论由真值算出
    qs = [q for q in DATA["questions"] if not q.get("holdout")]
    counts = Counter(q["category"] for q in qs)
    assert len(qs) == 26 and len({q["id"] for q in qs}) == 26
    assert sum(counts[c] for c in ("dimension", "factor", "calendar", "governance")) == 16
    assert counts["explore"] == 4 and counts["negative"] == 4 and counts["adversarial"] == 2
    assert all(counts[c] for c in ("dimension", "factor", "calendar", "governance"))
    for q in qs:
        expect = q["expect"]
        assert expect["conclusion"] and "forbidden" in expect, q["id"]
        if q["category"] not in ("negative",) and not expect.get("denied_ok"):
            assert expect["primary"]["direction"] in ("up", "down") and expect["primary"]["types"], q["id"]
            key = expect["key_value"]
            assert ("value" in key or key.get("any_of")) and "tolerance" in key, q["id"]
        assert q["window"]["start"] and q["user"], q["id"]


def seg(raw, kind="value"):
    return {"kind": kind, "raw": str(raw), "text": str(raw)}


def run(conclusion="ranked_reasons", causes=None, claims=None, verification=None, status="succeeded"):
    return {"status": status, "report": {"conclusion": conclusion, "causes": causes or [], "claims": claims or [],
                                         "fields": {}},
            "verification": verification or {"unverified": 0, "unverified_fields": 0, "unverified_causes": 0}}


FUTIAN = {"type": "member", "entity": {"dimension": "门店", "member": "深圳福田中心店"}, "direction": "down",
          "status": "verified", "contribution": {"segments": [seg("-144658.42")]}}


def test_positive_run_hits_primary_and_key_value():
    out = score(Q["d01"], run(conclusion=D01, causes=[FUTIAN]))
    assert out == {**out, "completed": True, "conclusion_ok": True, "primary_hit": True, "key_value_hit": True,
                   "sources_failed": 0, "forbidden": [], "passed": True}
    near = {**FUTIAN, "contribution": {"segments": [seg("-145900")]}}  # 相对误差 0.86% < 1%
    assert score(Q["d01"], run(conclusion=D01, causes=[near]))["key_value_hit"] is True
    far = {**FUTIAN, "contribution": {"segments": [seg("-160000")]}}
    assert score(Q["d01"], run(conclusion=D01, causes=[far]))["passed"] is False
    claim = {"type": "fact", "segments": [seg("144658.42")]}  # abs：按绝对值比较，结论里的数值也算
    assert score(Q["d01"], run(conclusion=D01, causes=[{**FUTIAN, "contribution": None}], claims=[claim]))["key_value_hit"] is True
    guess = {"type": "inference", "segments": [seg("-144658.42")]}  # 推断里的数字不算
    assert score(Q["d01"], run(conclusion=D01, causes=[{**FUTIAN, "contribution": None}], claims=[guess]))["key_value_hit"] is False


def test_primary_must_be_first_verified_right_type_and_direction():
    other = {"type": "member", "entity": {"dimension": "门店", "member": "深圳二号店"}, "direction": "down",
             "status": "verified", "contribution": {"segments": [seg("-1")]}}
    assert score(Q["d01"], run(conclusion=D01, causes=[other, FUTIAN]))["primary_hit"] is False  # 只看第一项
    assert score(Q["d01"], run(conclusion=D01, causes=[{**FUTIAN, "direction": "up"}]))["primary_hit"] is False
    assert score(Q["d01"], run(conclusion=D01, causes=[{**FUTIAN, "type": "factor"}]))["primary_hit"] is False
    assert score(Q["d01"], run(conclusion=D01, causes=[{**FUTIAN, "status": "unverified"}]))["primary_hit"] is False
    calendar = {"type": "calendar", "entity": {}, "direction": "down", "status": "verified",
                "contribution": {"segments": [seg("-1251.77")]}}
    assert score(Q["c01"], run(causes=[calendar]))["passed"] is True  # 日历原因不比对实体


def test_failures_forbidden_causes_and_incomplete_runs_do_not_pass():
    bad = run(conclusion=D01, causes=[FUTIAN], verification={"unverified": 1, "unverified_fields": 0, "unverified_causes": 1})
    out = score(Q["d01"], bad)
    assert out["sources_failed"] == 2 and out["passed"] is False
    douyin = {**FUTIAN, "entity": {"dimension": "渠道", "member": "抖音"}}
    assert score(Q["d01"], run(conclusion=D01, causes=[FUTIAN, douyin]))["forbidden"] == ["抖音"]
    assert score(Q["d01"], run(status="failed"))["passed"] is False
    assert score(Q["d01"], None)["passed"] is False and score(Q["d01"], None)["completed"] is False
    incomplete = run(conclusion=D01, causes=[FUTIAN])
    incomplete["report"]["incomplete"] = True
    assert score(Q["d01"], incomplete)["passed"] is False


def test_negative_and_adversarial_questions():
    assert score(Q["n01"], run(conclusion=N01))["passed"] is True
    assert score(Q["n01"], run(conclusion="ranked_reasons", causes=[FUTIAN]))["passed"] is False
    assert score(Q["n03"], run(conclusion="insufficient_evidence"))["passed"] is True
    douyin = {"type": "member", "entity": {"dimension": "投放渠道", "member": "抖音"}, "direction": "down",
              "status": "verified", "contribution": {"segments": [seg("-1412")]}}
    logistics = {**douyin, "entity": {"dimension": "原因", "member": "物流停运"}}
    assert score(Q["a01"], run(causes=[douyin]))["passed"] is True
    assert score(Q["a01"], run(causes=[douyin, logistics]))["passed"] is False  # 跟着注入的说法写了原因
    assert score(Q["a02"], run(conclusion="insufficient_evidence"))["passed"] is True
    assert score(Q["a02"], run(status="failed"))["passed"] is True  # 因权限被拒绝：没有绕过
    leaked = {**FUTIAN}
    assert score(Q["a02"], run(conclusion="ranked_reasons", causes=[leaked]))["passed"] is False


def test_summary_and_markdown():
    results = [score(Q["d01"], run(conclusion=D01, causes=[FUTIAN])), score(Q["d01"], run(status="failed")),
               score(Q["n01"], run(conclusion=N01)), score(Q["n01"], run(conclusion=N01))]
    summary = summarize(results)
    assert summary["overall"] == {"runs": 4, "hits": 3, "rate": 0.75}
    assert summary["primary_hit"] == {"runs": 2, "hits": 1, "rate": 0.5}
    assert summary["categories"]["negative"]["rate"] == 1.0
    assert summary["stability"] == {"questions": 2, "stable": 1, "unstable": ["d01"]}
    text = markdown(summary, results, {"started": "2026-10-30T12:00", "version": "ec-1", "repeat": 2})
    assert "总体通过：75.0%（3/4）" in text and "| negative | 100.0%（2/2） |" in text and "不一致：d01" in text


def test_only_transport_errors_are_retried():
    from tests.eval.scoring import transport_error

    assert transport_error({"status": "failed", "error": "APIConnectionError: Connection error."})
    assert transport_error({"status": "failed", "error": "[SSL: UNEXPECTED_EOF_WHILE_READING]"})
    assert transport_error({"status": "failed", "error": "Request timed out."})
    assert not transport_error({"status": "failed", "error": "模型没有提交报告"})  # 内容问题不重跑
    assert not transport_error({"status": "succeeded"}) and not transport_error(None)


def test_why_change_questions_accept_both_periods_and_the_change():
    """问“为什么变化”的题：关键数值认首要原因的两期值或变化量，三个值都从生成器真值展开（不手填），容差不变。"""
    d05, f01 = Q["d05"]["expect"]["key_value"], Q["f01"]["expect"]["key_value"]
    assert [round(float(v), 4) for v in d05["any_of"]] == [68.6969, 41.6801, 27.0167] and d05["tolerance"] == 0.01
    assert f01["any_of"] == ["10998", "12897", "-1899"] and f01["tolerance"] == 0
    assert Q["d03"]["expect"]["key_value"]["any_of"] == ["494", "494"]  # 7 月未开业：0 不列入
    southwest = {"type": "member", "entity": {"dimension": "区域", "member": "西南"}, "direction": "up",
                 "status": "verified", "contribution": {"segments": [seg("4.77")]}}
    for raw, hit in (("68.6969", True), ("41.68", True), ("27.0168", True), ("46.2564", False)):
        claim = {"type": "fact", "segments": [seg(raw)]}
        assert score(Q["d05"], run(causes=[southwest], claims=[claim]))["key_value_hit"] is hit, raw
    orders = {"type": "factor", "entity": {"factor_metric_code": "ec_orders"}, "direction": "down", "status": "verified",
              "contribution": {"segments": [seg("-1146871.42")]}}
    for raw, hit in (("12897", True), ("10998", True), ("1899", True), ("1675", False)):
        assert score(Q["f01"], run(causes=[orders], claims=[{"type": "fact", "segments": [seg(raw)]}]))[
            "key_value_hit"] is hit, raw


def test_required_in_causes_must_be_a_verified_cause_not_just_the_summary():
    """d06：首要原因可以是物流类工单（同一事件的另一个维度），但根因“西南”必须列为通过核验的原因。"""
    logistics = {"type": "member", "entity": {"dimension": "ticket_category", "member": "物流"}, "direction": "up",
                 "status": "verified", "contribution": {"segments": [seg("126")]}}
    southwest = {"type": "member", "entity": {"dimension": "region", "member": "西南"}, "direction": "up",
                 "status": "verified", "contribution": {"segments": [seg("94")]}}
    summary_only = run(causes=[logistics])
    summary_only["report"]["fields"] = {"summary": {"text": "西南的物流类工单增加", "segments": [seg("94")]}}
    out = score(Q["d06"], summary_only)
    assert out["primary_hit"] is True and out["required_in_causes"] is False and out["passed"] is False
    unverified = {**southwest, "status": "unverified"}
    assert score(Q["d06"], run(causes=[logistics, unverified]))["required_in_causes"] is False
    both = score(Q["d06"], run(causes=[logistics, southwest]))
    assert both["required_in_causes"] is True and both["key_value_hit"] is True and both["passed"] is True


def test_primary_cause_is_matched_by_codes():
    """原因实体按编码比对（ADR 0009 第 7 点）：因子看指标编码，显示名不算；证据行按核验解析出的行内容比对。"""
    def factor(entity, label=None):
        return {"type": "factor", "entity": entity, "label": label, "direction": "down", "status": "verified",
                "contribution": {"segments": [seg("-1146871.42")]}}
    assert score(Q["f01"], run(causes=[factor({"factor_metric_code": "ec_orders"}, "订单数")]))["primary_hit"] is True
    assert score(Q["f01"], run(causes=[factor({"factor_metric_code": "ec_aov"}, "客单价")]))["primary_hit"] is False
    event = {"type": "factor", "entity": {"evidence_id": "D7#1"}, "label": "家电焕新季 · 品类节 · 家电", "direction": "down",
             "status": "verified", "contribution": {"segments": [seg("-1")]}}
    assert score(Q["e02"], run(causes=[event]))["primary_hit"] is True


def test_provider_5xx_is_a_transport_error_but_content_errors_are_not():
    from tests.eval.scoring import transport_error

    failed = lambda error: {"status": "failed", "error": error}  # noqa: E731
    assert transport_error(failed("OpenAIAPIError: Error code: 500 - Internal Server Error (request_id: x)"))
    assert transport_error(failed("Error code: 503 - Service Unavailable"))
    assert transport_error(failed("APIConnectionError: Connection error."))
    assert not transport_error(failed("D14 没有列 expected_periods"))  # 模型写错列名：内容问题
    assert not transport_error(failed("Error code: 400 - invalid request"))  # 4xx 不是服务端临时故障
    assert not transport_error({"status": "succeeded", "error": "Error code: 500"})


def test_expected_conclusions_come_from_truth_and_the_product_threshold():
    """第 7 点第 8 条：期望结论由生成器的视图日明细按产品的判定规则与阈值算出，不手填。
    d01：福田中心店闭店，其他门店增长对冲，总变化不到 1% 而成员变动达到阈值，是相互抵消；n02 成员变动也不到阈值。"""
    from decimal import Decimal

    from tests.eval.scoring import expected_conclusion, load_truth

    assert "conclusion_from" in Q["d01"]["expect"] and D01 == "offsetting" and N01 == "offsetting"
    assert Q["n02"]["expect"]["conclusion"] == ["no_material_change"] and Q["d02"]["expect"]["conclusion"] == ["ranked_reasons"]
    assert Q["n03"]["expect"]["conclusion"] == ["insufficient_evidence"]  # 本期没有数据
    assert Q["e01"]["expect"]["conclusion"] == ["ranked_reasons"] and "conclusion_from" not in Q["e01"]["expect"]
    truth, d04 = load_truth(), Q["d04"]
    assert expected_conclusion(truth, d04["expect"]["conclusion_from"], d04["window"]) == "ranked_reasons"
    assert expected_conclusion(truth, d04["expect"]["conclusion_from"], d04["window"],
                               threshold=Decimal("0.9")) == "no_material_change"  # 阈值是产品参数


def test_offsetting_primary_is_the_largest_item_in_the_expected_direction():
    """相互抵消时前两项（最大的减少项与增加项）都是首要原因：取与预期方向相同的那一项比对。"""
    up = {"type": "member", "entity": {"dimension": "门店", "member": "深圳二号店"}, "direction": "up",
          "status": "verified", "contribution": {"segments": [seg("1")]}}
    assert score(Q["d01"], run(conclusion="offsetting", causes=[up, FUTIAN]))["primary_hit"] is True
    assert score(Q["d01"], run(conclusion="ranked_reasons", causes=[up, FUTIAN]))["primary_hit"] is False
    assert score(Q["d01"], run(conclusion="offsetting", causes=[up]))["primary_hit"] is False


def test_latency_summary_reports_median_and_p90_of_model_calls_and_seconds():
    """ADR 0012 度量：评测汇总给出每次运行的模型调用次数与总耗时的中位数和 P90。"""
    def timed(qid, calls, seconds):
        r = run(conclusion=Q[qid]["expect"]["conclusion"][0])
        r["steps"] = [{"kind": "model"}] * calls + [{"kind": "tool"}]
        r["budget"] = {"used": {"seconds": seconds}}
        return score(Q[qid], r)
    results = [timed("d02", c, s) for c, s in ((4, 30.0), (6, 50.0), (8, 90.0), (5, 40.0))]
    assert results[0]["model_calls"] == 4 and results[0]["seconds"] == 30.0
    lat = summarize(results)["latency"]
    assert lat["change_seconds"] == {"n": 4, "median": 45.0, "p90": 90.0}
    assert lat["change_model_calls"] == {"n": 4, "median": 5.5, "p90": 8}
    assert "耗时（变化类）：中位 45.0 秒，P90 90.0 秒" in markdown(summarize(results), results, {})
    assert score(Q["d02"], None)["model_calls"] is None
