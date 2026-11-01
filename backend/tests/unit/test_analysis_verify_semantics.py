"""文字识别符与分析方法的核验范围，不改变真实数字和结论的来源检查。"""

from copy import deepcopy

import pytest

from avelis.analysis.verify import ground_report

DATA = {
    "D5": {
        "kind": "governed",
        "columns": ["value"],
        "rows": [{"value": "1412"}],
        "units": {"value": {"kind": "level", "unit": "单"}},
    }
}
REPORT = {
    "title": ["分析结果"],
    "summary": ["数值为 ", {"dataset": "D5", "column": "value"}],
    "conclusion": "insufficient_evidence",
    "claims": [{"type": "fact", "parts": [{"dataset": "D5", "column": "value"}]}],
}


@pytest.mark.parametrize("identifier", ["V1", "P12", "D5", "D99", "v1", "AD9", "(V1)", "V1/P12"])
def test_ascii_identifiers_are_not_bare_numbers(identifier):
    report = {**REPORT, "method": [f"口径{identifier}处于受影响状态，核对（D5）"]}
    rendered, verification = ground_report(report, DATA, None)
    assert rendered["fields"]["method"]["status"] == "verified"
    assert verification["status"] == "all_passed" and verification["ok"]


@pytest.mark.parametrize(
    "text",
    [
        "9 月",
        "下降 9 单",
        "3 家",
        "V1 下降 9 单",
        "1e3",
        "1e+3",
        "1e-3",
        "1V1",
        "V1x",
        "V1.5",
        "V1,899",
        "V１",
        "V1２",
        "V 1",
    ],
)
def test_identifiers_do_not_hide_quantities_or_parts_of_numbers(text):
    rendered, verification = ground_report({**REPORT, "summary": [text]}, DATA, None)
    assert rendered["fields"]["summary"]["status"] == "unverified"
    assert rendered["fields"]["summary"]["errors"]
    assert verification["status"] == "failed" and not verification["ok"]


@pytest.mark.parametrize("reference", [{"dataset": "D99", "column": "value"}, "explicit"])
def test_identifier_exception_does_not_allow_missing_dataset_references(reference):
    claim = (
        {"type": "fact", "parts": ["见 D99"], "refs": ["D99"]}
        if reference == "explicit"
        else {"type": "fact", "parts": [reference]}
    )
    _, verification = ground_report({**REPORT, "claims": [claim]}, DATA, None)
    assert verification["status"] == "failed" and not verification["ok"]
    assert verification["claims"][0]["status"] == "unverified"


@pytest.mark.parametrize(
    "claims, status",
    [
        (REPORT["claims"], "all_passed"),
        ([*REPORT["claims"], {"type": "inference", "parts": ["可能另有原因"]}], "with_inference"),
        ([], "no_facts"),
    ],
)
def test_method_problems_are_visible_without_failing_overall_verification(claims, status):
    method = ["核对 V1、P12 与 D5，检查 9 月"]
    rendered, verification = ground_report({**REPORT, "method": method, "claims": claims}, DATA, None)
    record = rendered["fields"]["method"]
    assert record["parts"] == method and record["status"] == "unverified"
    assert "数字“9”" in record["errors"][0]
    assert verification["fields"][-1] == {"field": "method", "status": "unverified", "problems": record["errors"]}
    assert verification["unverified_fields"] == 1
    assert verification["ok"] and verification["status"] == status


def test_unresolved_method_reference_stays_a_field_problem():
    method = ["核对 ", {"dataset": "D99", "column": "value"}]
    rendered, verification = ground_report({**REPORT, "method": method}, DATA, None)
    record = rendered["fields"]["method"]
    assert record["status"] == "unverified" and record["errors"]
    assert verification["unverified_fields"] == 1
    assert verification["ok"] and verification["status"] == "all_passed"


@pytest.mark.parametrize("field", ["title", "summary", "unexplained"])
def test_other_report_fields_still_fail_verification(field):
    report = {**REPORT, "method": ["检查 9 月"], field: ["下降 9 单"]}
    _, verification = ground_report(report, DATA, None)
    assert verification["unverified_fields"] == 2
    assert not verification["ok"] and verification["status"] == "failed"


@pytest.mark.parametrize("component", ["claims", "causes", "conclusion"])
def test_conclusions_and_evidence_still_fail_verification(component):
    report = {**deepcopy(REPORT), "method": ["检查 9 月"]}
    if component == "claims":
        report["claims"][0]["parts"] = ["下降 9 单"]
        report["claims"][0]["refs"] = ["D5"]
    elif component == "causes":
        report["causes"] = [{"type": "calendar", "direction": "down", "evidence": ["D5"]}]
    else:
        report["conclusion"] = "ranked_reasons"
    _, verification = ground_report(report, DATA, None)
    assert verification["unverified_fields"] == 1
    assert not verification["ok"] and verification["status"] == "failed"
