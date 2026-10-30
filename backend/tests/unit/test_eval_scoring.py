"""评测集与判分（ADR 0009 第 5 点）：判分是纯函数，用手工构造的运行结果逐项核对。"""

from __future__ import annotations

from collections import Counter

from tests.eval.scoring import load_questions, markdown, score, summarize

DATA = load_questions()
Q = {q["id"]: q for q in DATA["questions"]}


def test_question_set_shape():
    qs = DATA["questions"]
    counts = Counter(q["category"] for q in qs)
    assert len(qs) == 20 and len({q["id"] for q in qs}) == 20
    assert sum(counts[c] for c in ("dimension", "factor", "calendar", "governance")) == 12
    assert counts["explore"] == 3 and counts["negative"] == 3 and counts["adversarial"] == 2
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
    out = score(Q["d01"], run(causes=[FUTIAN]))
    assert out == {**out, "completed": True, "conclusion_ok": True, "primary_hit": True, "key_value_hit": True,
                   "sources_failed": 0, "forbidden": [], "passed": True}
    near = {**FUTIAN, "contribution": {"segments": [seg("-145900")]}}  # 相对误差 0.86% < 1%
    assert score(Q["d01"], run(causes=[near]))["key_value_hit"] is True
    far = {**FUTIAN, "contribution": {"segments": [seg("-160000")]}}
    assert score(Q["d01"], run(causes=[far]))["passed"] is False
    claim = {"type": "fact", "segments": [seg("144658.42")]}  # abs：按绝对值比较，结论里的数值也算
    assert score(Q["d01"], run(causes=[{**FUTIAN, "contribution": None}], claims=[claim]))["key_value_hit"] is True
    guess = {"type": "inference", "segments": [seg("-144658.42")]}  # 推断里的数字不算
    assert score(Q["d01"], run(causes=[{**FUTIAN, "contribution": None}], claims=[guess]))["key_value_hit"] is False


def test_primary_must_be_first_verified_right_type_and_direction():
    other = {"type": "member", "entity": {"dimension": "门店", "member": "深圳二号店"}, "direction": "down",
             "status": "verified", "contribution": {"segments": [seg("-1")]}}
    assert score(Q["d01"], run(causes=[other, FUTIAN]))["primary_hit"] is False  # 只看第一项
    assert score(Q["d01"], run(causes=[{**FUTIAN, "direction": "up"}]))["primary_hit"] is False
    assert score(Q["d01"], run(causes=[{**FUTIAN, "type": "factor"}]))["primary_hit"] is False
    assert score(Q["d01"], run(causes=[{**FUTIAN, "status": "unverified"}]))["primary_hit"] is False
    calendar = {"type": "calendar", "entity": {}, "direction": "down", "status": "verified",
                "contribution": {"segments": [seg("-1251.77")]}}
    assert score(Q["c01"], run(causes=[calendar]))["passed"] is True  # 日历原因不比对实体


def test_failures_forbidden_causes_and_incomplete_runs_do_not_pass():
    bad = run(causes=[FUTIAN], verification={"unverified": 1, "unverified_fields": 0, "unverified_causes": 1})
    out = score(Q["d01"], bad)
    assert out["sources_failed"] == 2 and out["passed"] is False
    douyin = {**FUTIAN, "entity": {"dimension": "渠道", "member": "抖音"}}
    assert score(Q["d01"], run(causes=[FUTIAN, douyin]))["forbidden"] == ["抖音"]
    assert score(Q["d01"], run(status="failed"))["passed"] is False
    assert score(Q["d01"], None)["passed"] is False and score(Q["d01"], None)["completed"] is False
    incomplete = run(causes=[FUTIAN])
    incomplete["report"]["incomplete"] = True
    assert score(Q["d01"], incomplete)["passed"] is False


def test_negative_and_adversarial_questions():
    assert score(Q["n01"], run(conclusion="no_material_change"))["passed"] is True
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
    results = [score(Q["d01"], run(causes=[FUTIAN])), score(Q["d01"], run(status="failed")),
               score(Q["n01"], run(conclusion="no_material_change")), score(Q["n01"], run(conclusion="no_material_change"))]
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
    orders = {"type": "factor", "entity": {"factor": "订单数"}, "direction": "down", "status": "verified",
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
