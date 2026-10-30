"""报告的结构化原因与结论类型（ADR 0009 第 5、7 点）：原因项同样走核验，实体按编码关联；结论类型以 explain_change 为准。"""

from __future__ import annotations

from avelis.analysis.verify import ground_report

DATA = {
    "D1": {"kind": "governed", "columns": ["地区", "指标甲"], "rows": [{"地区": "甲", "指标甲": "100"}],
           "units": {"指标甲": {"kind": "level"}}, "metric_codes": ["m_a"]},
    "D2": {"kind": "calc", "metric_codes": ["m_a"], "columns": ["dimension", "member", "contribution", "ep"],
           "rows": [{"dimension": "地区", "member": "甲", "contribution": "-80", "ep": "0.8"},
                    {"dimension": "地区", "member": "乙", "contribution": "-20", "ep": "0.2"}],
           "units": {"contribution": {"kind": "delta"}, "ep": {"kind": "share"}}},
    "D3": {"kind": "context", "columns": ["type", "at"], "rows": [{"type": "元数据变更", "at": "2026-09-02"}]},
    "D4": {"kind": "calc", "metric_codes": ["m_a", "m_b", "m_r"], "columns": ["factor_metric_code", "factor", "contribution"],
           "rows": [{"factor_metric_code": "m_r", "factor": "单价", "contribution": "-30"},
                    {"factor_metric_code": None, "factor": "自定义因子", "contribution": "-5"}],
           "units": {"contribution": {"kind": "delta"}}},
    "D6": {"kind": "explore", "explore": True, "columns": ["门店", "变化"], "rows": [{"门店": "店甲", "变化": "-9"}],
           "units": {"变化": {"kind": "delta"}}},
    "D5": {"kind": "calc", "columns": ["value"], "rows": [{"value": "5"}], "summary": {"value": "5"},
           "summary_units": {"value": {"kind": "delta"}}, "assumption": True},
}
MEMBER = {"type": "member", "entity": {"metric_code": "m_a", "dimension": "地区", "member": "甲"}, "direction": "down",
          "contribution": {"dataset": "D2", "column": "contribution", "match": {"member": "甲"}}, "evidence": ["D2"]}


def run(conclusion, causes, metric_code=None, explanations=None):
    report = {"title": "t", "summary": "s", "claims": [{"type": "fact", "parts": ["x"], "refs": ["D1"]}],
              "conclusion": conclusion, "causes": causes, "metric_code": metric_code}
    return ground_report(report, DATA, None, explanations)


def problems(causes, conclusion="ranked_reasons", **kw):
    _, v = run(conclusion, causes, **kw)
    return [p for c in v["causes"] for p in c["problems"]] + v["conclusion"]["problems"]


def test_valid_member_factor_calendar_and_governance_causes():
    causes = [MEMBER,
              {"type": "factor", "entity": {"factor_metric_code": "m_r"}, "direction": "down",
               "contribution": {"dataset": "D4", "column": "contribution", "row": 1}, "evidence": ["D4"]},
              {"type": "governance", "entity": {"evidence_id": "D3#1"}, "direction": "down", "evidence": ["D3"]},
              {"type": "factor", "entity": {"evidence_id": "D4#2"}, "direction": "down",  # 没有口径编码的因子：证据行
               "contribution": {"dataset": "D4", "column": "contribution", "row": 2}, "evidence": ["D4"]}]
    rendered, v = run("ranked_reasons", causes)
    assert v["ok"] and v["unverified_causes"] == 0 and [c["status"] for c in v["causes"]] == ["verified"] * 4
    first = rendered["causes"][0]
    assert first["contribution"]["text"] == "-80" and first["entity"] == {"metric_code": "m_a", "dimension": "地区",
                                                                         "member": "甲"}
    assert [c["label"] for c in rendered["causes"]] == ["甲", "单价", "元数据变更 · 2026-09-02", "自定义因子"]  # 界面显示名称
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
    assert "成员 地区=丙 不在证据数据集里" in problems([{**MEMBER, "entity": {"metric_code": "m_a", "dimension": "地区",
                                                                               "member": "丙"},
                                                       "contribution": {"dataset": "D2", "column": "contribution", "row": 1}}])
    wrong_kind = problems([{**MEMBER, "evidence": ["D2", "D3"]}])
    assert any("事实类原因的证据必须是受治理查询、计算或探索结果" in p for p in wrong_kind)
    assert any("治理事件的证据 D2 必须是" in p for p in problems(
        [{"type": "governance", "entity": {"evidence_id": "D2#1"}, "direction": "down", "evidence": ["D2"]}]))
    assert any("证据行 D3#5 不存在" in p for p in problems(
        [{"type": "governance", "entity": {"evidence_id": "D3#5"}, "direction": "down", "evidence": ["D3"]}]))
    assert any("必须引用贡献数值" in p for p in problems(
        [{"type": "factor", "entity": {"factor_metric_code": "m_r"}, "direction": "down", "evidence": ["D4"]}]))
    assert any("需要写 entity.factor_metric_code 或 entity.evidence_id" in p for p in problems(
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



def test_entities_are_linked_by_codes_not_display_names():
    """第 7 点第 4 条：成员按 {metric_code, dimension, member}、因子按 factor_metric_code 在证据里找，不按显示名。"""
    factor = {"type": "factor", "direction": "down", "contribution": {"dataset": "D4", "column": "contribution", "row": 1},
              "evidence": ["D4"]}
    assert "因子指标 单价 不在证据数据集里" in problems([{**factor, "entity": {"factor_metric_code": "单价"}}])[0]  # 显示名不行
    assert "原因结构不合法" in problems([{**factor, "entity": {"factor": "单价"}}])[0]  # 旧字段已删除
    assert "指标 m_x 不在证据数据集的口径里" in problems(
        [{**MEMBER, "entity": {"metric_code": "m_x", "dimension": "地区", "member": "甲"}}])[0]
    explore = {"type": "member", "direction": "down", "contribution": {"dataset": "D6", "column": "变化", "row": 1},
               "evidence": ["D6"], "entity": {"metric_code": "m_a", "dimension": "门店", "member": "店甲"}}
    # 探索数据没有口径编码：成员的 metric_code 只能是所问指标
    assert "只能是所问指标" in problems([explore], metric_code="m_b", explanations={})[0]
    assert problems([explore], metric_code="m_a", explanations={"m_a": "ranked_reasons"}) == []


def test_conclusion_follows_explain_change_verdict():
    """第 7 点第 3 条：所问指标做过 explain_change 时，结论类型必须与它的判定一致（相互抵消按没有实质变化）。"""
    assert problems([], "no_material_change", metric_code="m_a", explanations={"m_a": "no_material_change"}) == []
    assert problems([], "no_material_change", metric_code="m_a", explanations={"m_a": "offsetting"}) == []
    assert problems([MEMBER], "ranked_reasons", metric_code="m_a", explanations={"m_a": "no_material_change"}) == [
        "explain_change 对 m_a 的判定是 no_material_change，结论类型应为 no_material_change（不是 ranked_reasons）"]
    assert "结论类型应为 ranked_reasons" in problems([], "no_material_change", metric_code="m_a",
                                                 explanations={"m_a": "ranked_reasons"})[0]
    # 做过自动归因却没写所问指标：要求写明；所问指标没做过 explain_change 却断言变化情况：要求先做
    assert "请在 metric_code 写明所问的指标" in problems([MEMBER], explanations={"m_a": "ranked_reasons"})[0]
    assert "请先对 m_a 调用 explain_change" in problems([MEMBER], metric_code="m_a", explanations={})[0]
    assert problems([], "insufficient_evidence", metric_code="m_a", explanations={}) == []
    # 不适用自动归因的指标（如不可加）：不约束
    assert problems([MEMBER], "ranked_reasons", metric_code="m_a", explanations={"m_a": "unsupported"}) == []
