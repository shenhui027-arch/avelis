"""报告的结构化原因与结论类型（ADR 0009 第 5、7 点）：原因项同样走核验，实体按编码关联；结论类型以 explain_change 为准。"""

from __future__ import annotations

from avelis.analysis.grounding import scope_key
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
    "D7": {"kind": "calc", "metric_codes": ["m_a"], "columns": ["dimension", "member", "contribution"],
           "rows": [{"dimension": "地区", "member": "甲", "contribution": "-80"},
                    {"dimension": "地区", "member": "丙", "contribution": "79"},
                    {"dimension": "地区", "member": "丁", "contribution": "-5"},
                    {"dimension": "地区", "member": "戊", "contribution": "3"}],
           "units": {"contribution": {"kind": "delta"}}},
    "D6": {"kind": "explore", "explore": True, "columns": ["门店", "变化"], "rows": [{"门店": "店甲", "变化": "-9"}],
           "units": {"变化": {"kind": "delta"}}},
    "D5": {"kind": "calc", "columns": ["value"], "rows": [{"value": "5"}], "summary": {"value": "5"},
           "summary_units": {"value": {"kind": "delta"}}, "assumption": True},
}
MEMBER = {"type": "member", "entity": {"metric_code": "m_a", "dimension": "地区", "member": "甲"}, "direction": "down",
          "contribution": {"dataset": "D2", "column": "contribution", "match": {"member": "甲"}}, "evidence": ["D2"]}


WINDOW = {"start": "2026-09-01", "end": "2026-09-30", "compare_start": "2026-08-01", "compare_end": "2026-08-31"}


def run(conclusion, causes, metric_code=None, explanations=None, filters=(), question_type=None):
    """explanations：指标编码 → 判定（无筛选、同一窗口），或已经是登记键 → 判定。"""
    report = {"title": "t", "summary": "s", "claims": [{"type": "fact", "parts": ["x"], "refs": ["D1"]}],
              "conclusion": conclusion, "causes": causes, "metric_code": metric_code,
              "scope_key": scope_key(metric_code, list(filters), WINDOW) if metric_code else None}
    keyed = {k if k.startswith("{") else scope_key(k, [], WINDOW): v for k, v in (explanations or {}).items()}
    return ground_report(report, DATA, None, keyed, question_type)


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
    md = export.to_markdown(view, audience="full")
    assert "## 主要原因" in md and "有排序的原因" in md
    assert "1. 甲" in md and "下降" in md and "2. 甲" in md and "上升" in md and "方向与贡献" in md
    assert "成员 · 地区：" not in md
    assert "下降 80" in md and "数字来源" not in md  # 贡献保留在原因正文，逐条来源由 CSV 提供。
    assert export.to_pdf(view, audience="full").startswith(b"%PDF")
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
    """第 7 点第 3 条：所问指标做过 explain_change 时，结论类型必须与它的判定一致（相互抵消是单独的结论类型）。"""
    assert problems([], "no_material_change", metric_code="m_a", explanations={"m_a": "no_material_change"}) == []
    assert problems([], "no_material_change", metric_code="m_a", explanations={"m_a": "offsetting"}) == [
        "explain_change 对 m_a（无筛选；本期 2026-09-01~2026-09-30，对比期 2026-08-01~2026-08-31）的判定是 offsetting，"
        "结论类型应为 offsetting（不是 no_material_change）"]
    assert "结论类型应为 no_material_change（不是 ranked_reasons）" in problems(
        [MEMBER], "ranked_reasons", metric_code="m_a", explanations={"m_a": "no_material_change"})[0]
    assert "结论类型应为 ranked_reasons" in problems([], "no_material_change", metric_code="m_a",
                                                 explanations={"m_a": "ranked_reasons"})[0]
    # 做过自动归因却没写 scope：要求写明；没有同范围的判定却断言变化情况：要求用这个 scope 调用 explain_change
    assert "请在 scope 写明所问的指标、筛选与窗口" in problems([MEMBER], explanations={"m_a": "ranked_reasons"})[0]
    assert "请用这个 scope" in problems([MEMBER], metric_code="m_a", explanations={})[0]
    assert problems([], "insufficient_evidence", metric_code="m_a", explanations={}) == []
    # 不适用自动归因的指标（如不可加）：不约束
    assert problems([MEMBER], "ranked_reasons", metric_code="m_a", explanations={"m_a": "unsupported"}) == []


def test_offsetting_lists_the_largest_decrease_and_increase_first():
    """第 7 点第 8 条：相互抵消时给出原因，前两项是最大的减少项与增加项（方向一减一增）；结论按 explain_change 核对。"""
    down = {"type": "member", "entity": {"metric_code": "m_a", "dimension": "地区", "member": "甲"}, "direction": "down",
            "contribution": {"dataset": "D7", "column": "contribution", "match": {"member": "甲"}}, "evidence": ["D7"]}
    up = {"type": "member", "entity": {"metric_code": "m_a", "dimension": "地区", "member": "丙"}, "direction": "up",
          "contribution": {"dataset": "D7", "column": "contribution", "match": {"member": "丙"}}, "evidence": ["D7"]}
    rendered, v = run("offsetting", [down, up], metric_code="m_a", explanations={"m_a": "offsetting"})
    assert v["ok"] and [c["status"] for c in v["causes"]] == ["verified", "verified"]
    assert rendered["conclusion"] == "offsetting"
    assert problems([down], "offsetting", metric_code="m_a", explanations={"m_a": "offsetting"}) == [
        "结论为 offsetting 时前两项原因应是最大的减少项与增加项（方向一减一增）"]
    assert problems([], "offsetting") == ["结论为 offsetting 时前两项原因应是最大的减少项与增加项（方向一减一增）"]
    assert "结论类型应为 ranked_reasons" in problems([down, up], "offsetting", metric_code="m_a",
                                                 explanations={"m_a": "ranked_reasons"})[0]


def test_conclusion_is_compared_only_with_the_same_scope():
    """第 7 点第 9 条：结论只和 scope（指标、规范化后的筛选与窗口）完全相同的 explain_change 比对。
    对整个指标的判定不能用来核对某个区域的结论；筛选的维度、取值顺序与重复不影响登记键。"""
    region = [{"dimension": "地区", "values": ["甲"]}]
    whole = {"m_a": "ranked_reasons"}  # 只对整个指标做过 explain_change
    out = problems([], "no_material_change", metric_code="m_a", explanations=whole, filters=region)
    assert len(out) == 1 and "没有与报告 scope m_a（地区=甲；" in out[0] and "请用这个 scope" in out[0]
    keyed = {scope_key("m_a", [{"dimension": "地区", "values": ["甲", "甲"]}], WINDOW): "no_material_change"}
    assert problems([], "no_material_change", metric_code="m_a", explanations=keyed, filters=region) == []
    assert scope_key("m_a", [{"dimension": "b", "values": ["2", "1"]}, {"dimension": "a", "values": ["x"]}], WINDOW) == \
        scope_key("m_a", [{"dimension": "a", "values": ["x"]}, {"dimension": "b", "values": ["1", "2", "1"]}], WINDOW)
    other = {**WINDOW, "compare_start": "2026-07-01", "compare_end": "2026-07-31"}
    assert scope_key("m_a", region, WINDOW) != scope_key("m_a", region, other)  # 窗口不同也是不同的范围
    # 不是“为什么变化”的报告（证据不足、没写 scope、没做过 explain_change）：不受约束
    assert problems([], "insufficient_evidence", explanations={}) == []


def test_change_questions_must_carry_a_scope_with_a_verdict():
    """理解阶段判为变化类的问题：报告省略 scope 不能绕过判定；没有同范围判定时不论结论类型都要求补做；
    判定登记为 unsupported 的照旧不约束。"""
    assert "请在 scope 写明" in problems([], "insufficient_evidence", question_type="change")[0]
    assert "请用这个 scope" in problems([], "insufficient_evidence", metric_code="m_a", question_type="change")[0]
    assert problems([], "insufficient_evidence", question_type="change", explanations={"m_a": "unsupported"}) == []
    assert problems([], "insufficient_evidence", question_type="relation") == []


def test_offsetting_first_two_must_be_the_largest_increase_and_decrease():
    """相互抵消：前两项按归因证据的贡献值核对，必须分别是最大的增加项与最大的减少项（不只看方向）。"""
    def member(name, direction):
        return {"type": "member", "entity": {"metric_code": "m_a", "dimension": "地区", "member": name},
                "direction": direction, "evidence": ["D7"],
                "contribution": {"dataset": "D7", "column": "contribution", "match": {"member": name}}}
    ok = {"m_a": "offsetting"}
    assert problems([member("甲", "down"), member("丙", "up")], "offsetting", metric_code="m_a", explanations=ok) == []
    wrong = problems([member("丁", "down"), member("丙", "up")], "offsetting", metric_code="m_a", explanations=ok)
    assert len(wrong) == 1 and "最大的减少项贡献是 -80，不是 -5" in wrong[0]
    small_up = problems([member("甲", "down"), member("戊", "up")], "offsetting", metric_code="m_a", explanations=ok)
    assert "最大的增加项贡献是 79" in small_up[0]


def test_method_is_rendered_checked_and_exported_like_other_fields():
    """分析方法（method）仍按片段渲染与核验，其数字保留在附录与引用来源 CSV 里。"""
    from avelis.analysis import export

    report = {"title": "t", "summary": "s", "conclusion": "insufficient_evidence", "causes": [],
              "claims": [{"type": "fact", "parts": ["x"], "refs": ["D1"]}],
              "method": ["先看地区甲的", {"dataset": "D1", "column": "指标甲"}, "，再看拆解"]}
    rendered, v = ground_report(report, DATA, None)
    assert rendered["method"] == "先看地区甲的100，再看拆解" and v["ok"]
    assert rendered["fields"]["method"]["segments"][1]["raw"] == "100"
    bad, v2 = ground_report({**report, "method": ["对比了两类数据"]}, DATA, None)
    assert v2["ok"] and v2["status"] == "all_passed" and v2["unverified_fields"] == 1
    assert bad["fields"]["method"]["status"] == "unverified"
    assert any(f["field"] == "method" for f in v2["fields"] if f["status"] == "unverified")
    view = {"id": "r", "question": "q", "status": "succeeded", "datasets": [], "steps": [], "report": rendered,
            "verification": v}
    md = export.to_markdown(view, audience="full")
    body, appendix = md.split("## 附录", 1)
    assert "分析方法" not in body
    assert appendix.lstrip().startswith("**分析方法**：先看地区甲的100，再看拆解")
    assert appendix.index("分析方法") < appendix.index("### 数据检查")
    assert "数字来源" not in md
    assert export.to_pdf(view, audience="full").startswith(b"%PDF")
    rows = export.sources_csv(view).decode("utf-8-sig").splitlines()
    assert any(r.startswith("分析方法,字段,100,") for r in rows)
