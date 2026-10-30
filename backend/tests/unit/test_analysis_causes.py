"""报告的结构化原因与结论类型（ADR 0009 第 5 点）：原因项同样走核验。"""

from __future__ import annotations

from avelis.analysis.verify import ground_report

DATA = {
    "D1": {"kind": "governed", "columns": ["地区", "指标甲"], "rows": [{"地区": "甲", "指标甲": "100"}],
           "units": {"指标甲": {"kind": "level"}}},
    "D2": {"kind": "calc", "columns": ["dimension", "member", "contribution", "ep"],
           "rows": [{"dimension": "地区", "member": "甲", "contribution": "-80", "ep": "0.8"},
                    {"dimension": "地区", "member": "乙", "contribution": "-20", "ep": "0.2"}],
           "units": {"contribution": {"kind": "delta"}, "ep": {"kind": "share"}}},
    "D3": {"kind": "context", "columns": ["type", "at"], "rows": [{"type": "元数据变更", "at": "2026-09-02"}]},
    "D4": {"kind": "calc", "columns": ["factor", "contribution"], "rows": [{"factor": "单价", "contribution": "-30"}],
           "units": {"contribution": {"kind": "delta"}}},
    "D5": {"kind": "calc", "columns": ["value"], "rows": [{"value": "5"}], "summary": {"value": "5"},
           "summary_units": {"value": {"kind": "delta"}}, "assumption": True},
}
MEMBER = {"type": "member", "entity": {"dimension": "地区", "member": "甲"}, "direction": "down",
          "contribution": {"dataset": "D2", "column": "contribution", "match": {"member": "甲"}}, "evidence": ["D2"]}


def run(conclusion, causes):
    report = {"title": "t", "summary": "s", "claims": [{"type": "fact", "parts": ["x"], "refs": ["D1"]}],
              "conclusion": conclusion, "causes": causes}
    return ground_report(report, DATA, None)


def problems(causes, conclusion="ranked_reasons"):
    _, v = run(conclusion, causes)
    return [p for c in v["causes"] for p in c["problems"]] + v["conclusion"]["problems"]


def test_valid_member_factor_calendar_and_governance_causes():
    causes = [MEMBER,
              {"type": "factor", "entity": {"factor": "单价"}, "direction": "down",
               "contribution": {"dataset": "D4", "column": "contribution"}, "evidence": ["D4"]},
              {"type": "governance", "entity": {"event": "D3#1"}, "direction": "down", "evidence": ["D3"]}]
    rendered, v = run("ranked_reasons", causes)
    assert v["ok"] and v["unverified_causes"] == 0 and [c["status"] for c in v["causes"]] == ["verified"] * 3
    first = rendered["causes"][0]
    assert first["contribution"]["text"] == "-80" and first["entity"] == {"dimension": "地区", "member": "甲"}
    assert first["evidence"] == ["D2"] and rendered["conclusion"] == "ranked_reasons"


def test_conclusion_must_match_causes():
    assert problems([], "ranked_reasons") == ["结论为 ranked_reasons 时必须给出至少一个原因"]
    assert problems([MEMBER], "no_material_change") == ["结论为 no_material_change 时不能给出原因排序"]
    assert "结论类型应为" in problems([], "maybe")[0]
    _, v = run("no_material_change", [])
    assert v["ok"] and v["conclusion"] == {"value": "no_material_change", "problems": []}
    _, v = run(None, [])
    assert not v["ok"]  # 结论类型必填


def test_cause_checks_reject_wrong_evidence_direction_and_entities():
    assert "方向与贡献的正负不一致" in problems([{**MEMBER, "direction": "up"}])[0]
    assert "成员 地区=丙 不在证据数据集里" in problems([{**MEMBER, "entity": {"dimension": "地区", "member": "丙"},
                                                       "contribution": {"dataset": "D2", "column": "contribution", "row": 1}}])
    wrong_kind = problems([{**MEMBER, "evidence": ["D2", "D3"]}])
    assert any("事实类原因的证据必须是受治理查询、计算或探索结果" in p for p in wrong_kind)
    assert any("治理事件的证据 D2 必须是" in p for p in problems(
        [{"type": "governance", "entity": {"event": "D2#1"}, "direction": "down", "evidence": ["D2"]}]))
    assert any("事件 D3#5 不存在" in p for p in problems(
        [{"type": "governance", "entity": {"event": "D3#5"}, "direction": "down", "evidence": ["D3"]}]))
    assert any("必须引用贡献数值" in p for p in problems(
        [{"type": "factor", "entity": {"factor": "单价"}, "direction": "down", "evidence": ["D4"]}]))
    assert any("需要写 entity.factor" in p for p in problems(
        [{"type": "factor", "entity": {}, "direction": "down", "contribution": {"dataset": "D4", "column": "contribution"},
          "evidence": ["D4"]}]))
    assert any("证据数据集 D9 不存在" in p for p in problems([{**MEMBER, "evidence": ["D9"]}]))
    assert any("贡献来自假设计算" in p for p in problems(
        [{"type": "calendar", "entity": {}, "direction": "up", "contribution": {"dataset": "D5", "summary": "value"},
          "evidence": ["D5"]}]))
    assert any("条件选中了 0 行" in p for p in problems(
        [{**MEMBER, "contribution": {"dataset": "D2", "column": "contribution", "match": {"member": "丁"}}}]))
    assert "原因结构不合法" in problems([{**MEMBER, "score": 1}])[0]
    _, v = run("ranked_reasons", [MEMBER, {**MEMBER, "direction": "up"}])
    assert not v["ok"] and v["unverified_causes"] == 1 and v["causes"][0]["status"] == "verified"


def test_exports_show_cause_ranking():
    from avelis.analysis import export

    rendered, verification = run("ranked_reasons", [MEMBER, {**MEMBER, "direction": "up"}])
    view = {"id": "r", "question": "q", "status": "succeeded", "datasets": [], "steps": [], "report": rendered,
            "verification": verification}
    md = export.to_markdown(view)
    assert "## 原因排序" in md and "结论类型：有排序的原因" in md
    assert "1. 成员 · 地区：甲 · 下降 · 贡献 -80（证据 D2）" in md and "2. 成员 · 地区：甲 · 上升" in md and "⚠ 方向与贡献" in md
    assert "原因 1（原因）“-80”" in md  # 数值来源附录也列出原因的贡献
    assert export.to_pdf(view).startswith(b"%PDF")
    rows = export.sources_csv(view).decode("utf-8-sig").splitlines()
    assert any(r.startswith("原因 1,原因,-80,") for r in rows)
