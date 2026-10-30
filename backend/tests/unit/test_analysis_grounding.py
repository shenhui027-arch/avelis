"""报告数字的引用片段与结构检查（ADR 0009 第 1、2 点）：渲染结果与问题列表逐项断言。"""

from __future__ import annotations

import pytest

from avelis.analysis.grounding import NumberRef, WindowRef, render
from avelis.analysis.verify import ground_report

DATA = {
    "D1": {"kind": "governed", "columns": ["区域", "门店", "成交额", "变化率"],
           "rows": [{"区域": "华东", "门店": "广州10号店", "成交额": "1969398.05", "变化率": "-0.1472"},
                    {"区域": "华南", "门店": "深圳福田中心店", "成交额": "300071.87", "变化率": "0.0587"}]},
    "D2": {"kind": "calc", "columns": ["name", "value"], "rows": [{"name": "diff", "value": "-1675.0000"}],
           "summary": {"label": "差额", "value": "-1675.0000", "share": "0.8820"}},
    "D3": {"kind": "governed", "columns": ["门店"], "rows": [{"门店": "A"}, {"门店": "B"}], "truncated": True},
    "D4": {"kind": "governed", "columns": ["成交额", "日期"], "rows": [{"成交额": 0.125, "日期": "2026-09-10"}]},
}
WINDOW = {"start": "2026-09-01", "end": "2026-09-30", "compare_start": "2026-08-02", "compare_end": "2026-08-31"}


def text(*parts, data=DATA, window=WINDOW):
    out = render(list(parts), data, window)
    assert not out["errors"], out["errors"]
    return out["text"]


def errors(*parts, data=DATA, window=WINDOW):
    return render(list(parts), data, window)["errors"]


def ref(**kw):
    return {"dataset": "D1", **kw}


def test_formats_round_half_up_and_strip_zeros():
    one = {"dataset": "D4", "column": "成交额"}
    assert text(one) == "0.125"
    assert text({**one, "format": "d2"}) == "0.13"  # ROUND_HALF_UP，不是银行家舍入
    assert text({**one, "format": "int"}) == "0"
    assert text({**one, "format": "pct1"}) == "12.5%"
    assert text({**one, "format": "pp0"}) == "13个百分点"
    assert text(ref(column="成交额", row=1, format="wan1")) == "196.9万"
    assert text(ref(column="成交额", row=1, format="yi2")) == "0.02亿"
    assert text({"dataset": "D2", "value": "summary"}) == "-1675"  # 去掉多余的零
    assert text({"dataset": "D2", "value": "summary", "abs": True, "format": "d1"}) == "1675.0"
    assert text({"dataset": "D2", "value": "summary", "column": "share", "format": "pct1"}) == "88.2%"


def test_row_selection_by_match_row_or_single_row():
    assert text(ref(column="门店", row=1)) == "广州10号店"  # 带数字的名称原样显示
    assert text(ref(column="成交额", match={"区域": "华南"})) == "300071.87"
    assert text({"dataset": "D2", "column": "value"}) == "-1675"  # 只有一行
    assert errors(ref(column="成交额")) == ["D1.成交额：D1 有 2 行，请用 match 或 row 选定一行"]
    assert errors(ref(column="成交额", match={"区域": "西北"})) == [
        "D1.成交额[区域=西北]：条件选中了 0 行，必须恰好一行（求和请先用 derive）"]
    assert "没有列 地区" in errors(ref(column="成交额", match={"地区": "华东"}))[0]
    assert "没有列 销量" in errors(ref(column="销量", row=1))[0]
    assert errors(ref(column="成交额", row=3)) == ["D1.成交额[#3]：D1 只有 2 行"]
    assert "match 与 row 只能写一个" in errors(ref(column="成交额", row=1, match={"区域": "华东"}))[0]
    assert errors({"dataset": "D9", "column": "x"}) == ["D9.x：数据集 D9 不存在"]


def test_rows_distinct_and_summary_values():
    assert text(ref(value="rows")) == "2"
    assert text(ref(value="distinct:区域")) == "2"
    assert "已截断" in errors({"dataset": "D3", "value": "rows"})[0]
    assert "已截断" in errors({"dataset": "D3", "value": "distinct:门店"})[0]
    assert "没有汇总值" in errors(ref(value="summary"))[0]
    assert "value 只能是" in errors(ref(column="成交额", row=1, value="sum"))[0]


def test_window_range_and_month():
    assert text({"window": "current"}) == "2026-09-01 ~ 2026-09-30"
    assert text({"window": "current", "format": "month"}) == "2026 年 9 月"
    assert "不是一个整月" in errors({"window": "compare", "format": "month"})[0]  # 8 月 2 日起
    assert "没有对比期窗口" in errors({"window": "compare"}, window={"start": "2026-09-01", "end": "2026-09-30"})[0]


def test_non_numeric_values_and_malformed_refs():
    assert text({"dataset": "D4", "column": "日期"}) == "2026-09-10"  # 日期单元格原样
    assert "不是数值" in errors(ref(column="门店", row=1, format="d2"))[0]
    assert "不是数值" in errors(ref(column="门店", row=1, abs=True))[0]
    assert "结构不合法" in errors({"dataset": "D1", "column": "成交额", "colour": "red"})[0]  # extra="forbid"
    assert "结构不合法" in errors({"dataset": "<b>", "column": "x"})[0]
    assert "结构不合法" in errors({"dataset": "D1", "column": "成交额", "row": 1, "format": "pct9"})[0]
    assert isinstance(NumberRef.model_validate(ref(column="x", row=1)), NumberRef)
    assert WindowRef.model_validate({"window": "current"}).format == "range"


def check(claim_parts, kind="fact", refs=None, **extra):
    report = {"title": "标题", "summary": "摘要", "unexplained": None,
              "claims": [{"type": kind, "parts": claim_parts, "refs": refs or [], **extra}]}
    rendered, verification = ground_report(report, DATA, WINDOW)
    return rendered["claims"][0], verification["claims"][0], verification


def test_rule1_unresolvable_ref_is_unverified():
    claim, result, v = check(["西北 ", ref(column="成交额", match={"区域": "西北"})])
    assert result["status"] == "unverified" and not v["ok"] and claim["text"] == "西北 [?]"
    assert result["problems"] == ["D1.成交额[区域=西北]：条件选中了 0 行，必须恰好一行（求和请先用 derive）"]


@pytest.mark.parametrize("words, digit", [("共 3 家", "3"), ("同比 ３％", "３"), ("2026-09-15 当天", "2026-09-15"),
                                          ("见（D19）", "19"), ("Top3 门店", "3")])
def test_rule2_no_arabic_digits_in_text_parts(words, digit):
    _, result, _ = check([words, ref(column="成交额", row=1)])
    assert result["status"] == "unverified"
    assert result["problems"] == [f"文字片段里出现数字“{digit}”：数字请写成引用片段（日期用 window 引用或数据集里的日期）"]


def test_rule3_chinese_numerals_with_frozen_whitelist():
    for words in ("共三家门店", "两家门店", "九月下滑", "两期基本持平", "壹佰万", "营收千万元"):
        _, result, _ = check([words, ref(column="成交额", row=1)])
        assert result["status"] == "unverified", words
        assert result["problems"][0].startswith("文字片段里出现中文数字"), words
    for words in ("两期相比，", "两期合计 ", "两者之差 ", "口径统一、结果一致，是原因之一，", "单位（万元）"):
        _, result, _ = check([words, ref(column="成交额", row=1)])
        assert result["status"] == "verified", words


def test_rule4_direction_must_match_sign_for_abs_values():
    down = {"dataset": "D2", "value": "summary", "abs": True}
    assert check(["订单下降 ", down])[1]["status"] == "verified"
    _, bad, _ = check(["订单增长 ", down])
    assert bad["problems"] == ["方向与数值符号不一致：“增长 1675”的原值是 -1675.0000（D2.summary）"]
    up = ref(column="变化率", match={"区域": "华南"}, abs=True, format="pct1")
    assert check(["变化率提高 ", up])[1]["status"] == "verified"
    assert check(["变化率下降 ", up])[1]["status"] == "unverified"
    assert check(["先增长后下降 ", down])[1]["status"] == "verified"  # 取最近的方向词
    assert check(["订单增长了很多，后来又有变化 ", down])[1]["status"] == "verified"  # 超过 8 个字不看


def test_rule5_refs_are_union_and_must_exist():
    claim, result, _ = check(["华东 ", ref(column="成交额", row=1)], refs=["D2"])
    assert claim["refs"] == ["D2", "D1"] and claim["explicit_refs"] == ["D2"] and result["status"] == "verified"
    _, none, _ = check(["福田店关店"])
    assert none["problems"] == ["没有引用数据集：事实与计算类结论必须引用（显式 refs 或引用片段）"]
    _, missing, _ = check(["福田店关店"], refs=["D9"])
    assert missing["problems"] == ["引用了不存在的数据集 D9"] and missing["missing_refs"] == ["D9"]


def test_proposal_only_in_inference_or_recommendation():
    target = ref(column="成交额", row=1, role="proposal", format="wan1")
    _, fact, _ = check(["目标 ", target])
    assert fact["problems"] == ["D1.成交额[#1] 标为建议目标值（proposal），只能用在推断、建议类结论中"]
    claim, rec, v = check(["把目标定为 ", target], kind="recommendation", validation="下月复盘 3 次")
    assert rec["status"] == "unchecked" and v["ok"] and claim["segments"][1]["role"] == "proposal"


def test_inference_and_premise_allow_numbers_and_still_render():
    claim, result, v = check(["可能约 3 成来自促销，", ref(column="门店", row=2)], kind="inference",
                             premise=["促销力度与上期相近（约 ", ref(column="变化率", row=2, format="pct1"), "）"])
    assert result["status"] == "unchecked" and result["problems"] == [] and v["ok"]
    assert claim["text"] == "可能约 3 成来自促销，深圳福田中心店" and claim["premise"] == "促销力度与上期相近（约 5.9%）"
    _, broken, _ = check(["可能 ", ref(column="不存在", row=1)], kind="inference")
    assert broken["status"] == "unchecked" and "没有列 不存在" in broken["problems"][0]


def test_fields_are_rendered_and_checked_like_claims():
    report = {"title": [{"window": "current", "format": "month"}, "华东成交额下降"],
              "summary": ["华东成交额 ", ref(column="成交额", row=1, format="wan2"), "元，约 9 成来自线上"],
              "unexplained": "无", "claims": [{"type": "fact", "parts": ["x"], "refs": ["D1"]}]}
    rendered, v = ground_report(report, DATA, WINDOW)
    assert rendered["title"] == "2026 年 9 月华东成交额下降" and rendered["summary"] == "华东成交额 196.94万元，约 9 成来自线上"
    assert rendered["parts"]["title"] == report["title"] and rendered["segments"]["summary"][1]["raw"] == "1969398.05"
    assert [f["status"] for f in v["fields"]] == ["verified", "unverified", "verified"]
    assert v["fields"][1]["problems"] == ["文字片段里出现数字“9”：数字请写成引用片段（日期用 window 引用或数据集里的日期）"]
    assert not v["ok"] and v["unverified"] == 0 and v["unverified_fields"] == 1


def test_incomplete_report_is_kept_as_is():
    report = {"title": "分析未完成", "summary": "分析停止（预算 3 次已用完）", "claims": [], "unexplained": "全部",
              "incomplete": True}
    rendered, v = ground_report(report, DATA, WINDOW)
    assert rendered == report and v["ok"] is False and v["claims"] == [] and v["fields"] == []
