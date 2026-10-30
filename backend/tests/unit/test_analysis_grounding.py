"""报告数字的引用片段、量纲与结构检查（ADR 0009 第 1、2 点）：渲染结果与问题列表逐项断言。"""

from __future__ import annotations

import pytest

from avelis.analysis.grounding import CountRef, LabelRef, NumberRef, WindowRef, render
from avelis.analysis.verify import ground_report

LEVEL = {"kind": "level", "unit": "元"}
DATA = {
    "D1": {"kind": "governed", "columns": ["区域", "门店", "成交额", "成交额·对比期", "成交额·变化", "成交额·变化率"],
           "rows": [{"区域": "华东", "门店": "广州10号店", "成交额": "1969398.05", "成交额·对比期": "2000000",
                     "成交额·变化": "-30601.95", "成交额·变化率": "-0.0153"},
                    {"区域": "华南", "门店": "深圳福田中心店", "成交额": "300071.87", "成交额·对比期": "250000",
                     "成交额·变化": "50071.87", "成交额·变化率": "0.2003"},
                    {"区域": "西南", "门店": "", "成交额": "10", "成交额·对比期": None, "成交额·变化": None,
                     "成交额·变化率": None}],
           "units": {"成交额": LEVEL, "成交额·对比期": LEVEL, "成交额·变化": {"kind": "delta", "unit": "元"},
                     "成交额·变化率": {"kind": "rate_change"}},
           "window": {"start": "2026-09-01", "end": "2026-09-30"}},
    "D2": {"kind": "calc", "columns": ["name", "value"], "rows": [{"name": "客单价", "value": "1.0587"}],
           "summary": {"value": "1.0587", "total": {"change": "-1675.0000", "share": "0.8820"}},
           "summary_units": {"value": {"kind": "ratio"}, "total.change": {"kind": "delta", "unit": "单"},
                             "total.share": {"kind": "share"}}},
    "D3": {"kind": "governed", "columns": ["门店"], "rows": [{"门店": "A"}, {"门店": "B"}], "truncated": True},
    "D4": {"kind": "governed", "columns": ["成交额", "日期", "转化率·变化"],
           "rows": [{"成交额": "1.25", "日期": "2026-09-10", "转化率·变化": "-0.012"}],
           "units": {"成交额": {"kind": "level", "unit": "元", "scale": "10000"},
                     "转化率·变化": {"kind": "delta", "of_ratio": True}}},
    "D5": {"kind": "calc", "columns": ["value"], "rows": [{"value": "0.5"}], "summary": {"value": "0.5"},
           "summary_units": {"value": {"kind": "unknown"}}, "assumption": True},
    "D6": {"kind": "explore", "columns": ["客流"], "rows": [{"客流": "1200"}], "explore": True},
}
WINDOW = {"start": "2026-09-01", "end": "2026-09-30", "compare_start": "2026-08-02", "compare_end": "2026-08-31"}


def text(*parts, data=DATA, window=WINDOW):
    out = render(list(parts), data, window)
    assert not out["errors"], out["errors"]
    return out["text"]


def errors(*parts, data=DATA, window=WINDOW):
    return render(list(parts), data, window)["errors"]


def cell(column="成交额", **kw):
    return {"dataset": "D1", "column": column, **kw}


def test_units_come_from_the_formatter_and_formats_follow_kind():
    huadong = cell(match={"区域": "华东"})
    assert text(huadong) == "1969398.05元"
    assert text({**huadong, "format": "wan2"}) == "196.94万元"
    assert text({**huadong, "format": "yi2"}) == "0.02亿元"
    assert text({**huadong, "format": "int"}) == "1969398元"
    assert text({"dataset": "D4", "column": "成交额", "format": "d1"}) == "1.3万元"  # 列本身是万元：显示带“万元”
    assert text({"dataset": "D4", "column": "成交额", "format": "wan2"}) == "1.25万元"  # 先按 unit_scale 换算回元
    assert text(cell("成交额·变化率", match={"区域": "华南"}, format="pct1")) == "+20.0%"  # 没写方向：带符号
    assert text({"dataset": "D2", "summary": "value", "format": "x2"}) == "1.06倍"
    assert text({"dataset": "D2", "summary": "total.share", "format": "pct1"}) == "88.2%"
    assert text({"dataset": "D4", "column": "转化率·变化", "format": "pp1"}) == "-1.2个百分点"
    assert errors({**huadong, "format": "pct1"}) == [
        "第 1 个片段（D1.成交额[区域=华东]）：格式 pct1 不能用于量纲 level（pct 只用于 rate_change、share）"]
    assert "x 只用于 ratio" in errors({**huadong, "format": "x1"})[0]
    assert "只用于比率差" in errors(cell("成交额·变化", row=1, format="pp1"))[0]
    assert "不能用于量纲 unknown" in errors({"dataset": "D6", "column": "客流", "format": "wan1"})[0]
    assert text({"dataset": "D6", "column": "客流"}) == "1200"  # unknown：不带单位


def test_direction_is_checked_and_shown_as_absolute_value():
    down = cell("成交额·变化", match={"区域": "华东"}, direction="down", format="d0")
    assert text("下降 ", down) == "下降 30602元"
    assert text(cell("成交额·变化", match={"区域": "华东"})) == "-30601.95元"  # 没写方向：带符号
    assert errors(cell("成交额·变化", match={"区域": "华东"}, direction="up")) == [
        "第 1 个片段（D1.成交额·变化[区域=华东]）：方向与正负不一致：direction=up，原值 -30601.95"]
    assert "不能标方向" in errors(cell(match={"区域": "华东"}, direction="down"))[0]  # 水平值不做方向
    assert text({"dataset": "D2", "summary": "total.change", "direction": "down"}) == "1675单"


def test_row_selection_and_summary_paths():
    assert text(cell(match={"区域": "华南"})) == "300071.87元"
    assert text(cell(row=2, format="d0")) == "300072元"
    assert errors(cell()) == ["第 1 个片段（D1.成交额）：D1 有 3 行，请用 match 或 row 选定一行"]
    assert errors(cell(match={"区域": "西北"})) == [
        "第 1 个片段（D1.成交额[区域=西北]）：条件选中了 0 行，必须恰好一行（求和请先用 derive）"]
    assert "没有列 地区" in errors(cell(match={"地区": "华东"}))[0]
    assert "只有 3 行" in errors(cell(row=4))[0]
    assert "match 与 row 只能写一个" in errors(cell(row=1, match={"区域": "华东"}))[0]
    assert "没有登记汇总路径 total.previous" in errors({"dataset": "D2", "summary": "total.previous"})[0]
    assert "column 与 summary 必须且只能写一个" in errors({"dataset": "D2", "column": "value", "summary": "value"})[0]
    assert "不是有限数值" in errors(cell("成交额·对比期", match={"区域": "西南"}))[0]
    assert "数据集 D9 不存在" in errors({"dataset": "D9", "column": "x"})[0]


def test_label_count_and_window_refs():
    assert text({"dataset": "D1", "label": "门店", "row": 1}) == "广州10号店"
    assert "是数值列，不是维度列" in errors({"dataset": "D1", "label": "成交额", "row": 1})[0]
    assert "不是有限数值" in errors(cell("门店", row=1))[0]  # 文本列不能当数值引用
    assert text({"dataset": "D1", "distinct": "门店"}) == "2"  # 不计空串
    assert text({"dataset": "D1", "distinct": "区域"}) == "3"
    assert "已截断" in errors({"dataset": "D3", "distinct": "门店"})[0]
    assert text({"window": "run_current"}) == "2026-09-01 ~ 2026-09-30"
    assert text({"window": "run_current", "format": "month"}) == "2026 年 9 月"
    assert text({"window": "dataset:D1", "format": "month"}) == "2026 年 9 月"
    assert "不是一个完整的单月" in errors({"window": "run_compare", "format": "month"})[0]
    assert "没有对比期窗口" in errors({"window": "run_compare"}, window={"start": "2026-09-01", "end": "2026-09-30"})[0]
    assert "不是带查询窗口" in errors({"window": "dataset:D2"})[0]
    assert text({"dataset": "D4", "label": "日期", "row": 1}) == "2026-09-10"  # 日期单元格原样


def test_malformed_parts_are_reported_by_position():
    assert errors("文字", {"dataset": "D1", "column": "成交额", "colour": "red"})[0].startswith("第 2 个片段：引用")
    assert "结构不合法" in errors({"dataset": "<b>", "column": "x"})[0]
    assert "结构不合法" in errors(cell(row=1, format="pct9"))[0]
    assert "结构不合法" in errors({"window": "current"})[0]
    assert isinstance(NumberRef.model_validate(cell(row=1)), NumberRef)
    assert isinstance(LabelRef.model_validate({"dataset": "D1", "label": "门店", "row": 1}), LabelRef)
    assert CountRef.model_validate({"dataset": "D1", "distinct": "门店"}).distinct == "门店"
    assert WindowRef.model_validate({"window": "run_current"}).format == "range"


def check(parts, kind="fact", refs=None, **extra):
    report = {"title": "标题", "summary": "摘要", "unexplained": None,
              "claims": [{"type": kind, "parts": parts, "refs": refs or [], **extra}]}
    rendered, verification = ground_report(report, DATA, WINDOW)
    return rendered["claims"][0], verification["claims"][0], verification


HUADONG = cell(match={"区域": "华东"}, format="wan2")


def test_rule2_value_segments_cannot_be_concatenated_or_followed_by_units():
    for between in ("", " ", ".", ",", "e", "+", "-", "×", " . "):
        _, result, _ = check([HUADONG, between, HUADONG] if between else [HUADONG, HUADONG])
        assert result["status"] == "unverified", repr(between)
        assert "两个数值段之间只隔着" in result["problems"][0], repr(between)
    assert check([HUADONG, "，相比 ", HUADONG])[1]["status"] == "verified"
    for suffix in ("%", "倍", "成", "元", "万", "亿", "个百分点", "折", " 元"):
        _, result, _ = check([HUADONG, suffix + "左右"])
        assert result["status"] == "unverified", suffix
        assert "后面紧跟单位或数量后缀" in result["problems"][0], suffix
    assert check([HUADONG, "，较上期下降"])[1]["status"] == "verified"


@pytest.mark.parametrize("words, digit", [("共 3 家", "3"), ("同比 ３％", "３"), ("2026-09-15 当天", "2026-09-15"),
                                          ("见（D19）", "19"), ("Top3 门店", "3")])
def test_rule3_no_arabic_digits_in_text_parts(words, digit):
    _, result, _ = check([words, "，", HUADONG])
    assert result["problems"] == [f"第 1 个片段（文字）出现数字“{digit}”：数字请写成引用对象（日期用 WindowRef 或数据集里的日期）"]


def test_rule4_chinese_quantities_with_final_whitelist():
    for words in ("共三家门店", "两家门店", "九月下滑", "两期基本持平", "壹佰万", "营收千万元", "前五家门店", "一度下滑",
                  "百分比很高"):
        _, result, _ = check([words, "，", HUADONG])
        assert result["status"] == "unverified", words
        assert "出现中文数字" in result["problems"][0], words
    for words in ("增长一倍", "翻番", "接近半数", "打八折"):
        _, result, _ = check([words, "，", HUADONG])
        assert any("数量词" in p for p in result["problems"]), words
    for words in ("两期相比，", "两期合计，", "两者之差，", "口径统一、结果一致，是原因之一，", "十分明显，"):
        assert check([words, HUADONG])[1]["status"] == "verified", words


def test_rule5_refs_are_union_and_must_exist():
    claim, result, _ = check(["华东 ", HUADONG], refs=["D2"])
    assert claim["refs"] == ["D2", "D1"] and claim["explicit_refs"] == ["D2"] and result["status"] == "verified"
    assert check(["福田店关店"])[1]["problems"] == ["没有引用数据集：事实与计算类结论必须引用（显式 refs 或引用片段）"]
    _, missing, _ = check(["福田店关店"], refs=["D9"])
    assert missing["problems"] == ["引用了不存在的数据集 D9"] and missing["missing_refs"] == ["D9"]


def test_proposal_and_assumption_only_in_inference_or_recommendation():
    target = cell(match={"区域": "华东"}, role="proposal", format="wan1")
    assert check(["目标 ", target])[1]["problems"] == [
        "第 2 个片段（D1.成交额[区域=华东]）标为建议目标值（proposal），只能用在推断、建议类结论中"]
    claim, rec, v = check(["把目标定为 ", target], kind="recommendation", validation="下月复盘 3 次")
    assert rec["status"] == "unchecked" and v["ok"] and claim["segments"][1]["role"] == "proposal"
    guess = {"dataset": "D5", "summary": "value"}
    assert "来自假设计算" in check(["估计 ", guess])[1]["problems"][0]
    assert check(["估计 ", guess], kind="inference")[1]["status"] == "unchecked"


def test_explore_sources_are_marked_and_counted():
    claim, result, v = check(["客流 ", {"dataset": "D6", "column": "客流"}, "，成交额 ", HUADONG])
    seg = claim["segments"][1]
    assert result["status"] == "verified" and seg["explore"] is True and seg["source"] == "explore"
    assert seg["label"] == "D6 · 客流" and claim["segments"][3]["label"] == "D1 · 成交额 · 区域=华东"
    assert v["sources"] == {"governed": 1, "explore": 1, "calc": 0, "context": 0}


def test_inference_and_details_allow_numbers_and_still_render():
    claim, result, v = check(["可能约 3 成来自促销，", {"dataset": "D1", "label": "门店", "row": 2}], kind="inference",
                             premise=["促销力度与上期相近（约 ", cell("成交额·变化率", row=2, format="pct1"), "）"])
    assert result["status"] == "unchecked" and result["problems"] == [] and v["ok"]
    assert claim["text"] == "可能约 3 成来自促销，深圳福田中心店" and claim["premise"] == "促销力度与上期相近（约 +20.0%）"
    assert claim["details"]["premise"]["status"] == "unchecked" and claim["details"]["premise"]["segments"][1]["kind"] == "value"
    assert "没有列 不存在" in check(["可能 ", cell("不存在", row=1)], kind="inference")[1]["problems"][0]


def test_fields_records_and_overall_status():
    report = {"title": [{"window": "run_current", "format": "month"}, "华东成交额下降"],
              "summary": ["华东成交额 ", HUADONG, "，约 9 成来自线上"], "unexplained": "无",
              "claims": [{"type": "fact", "parts": ["华东 ", HUADONG]}]}
    rendered, v = ground_report(report, DATA, WINDOW)
    assert rendered["title"] == "2026 年 9 月华东成交额下降" and rendered["summary"] == "华东成交额 196.94万元，约 9 成来自线上"
    title = rendered["fields"]["title"]
    assert title["parts"] == report["title"] and title["status"] == "verified" and title["errors"] == []
    assert rendered["fields"]["summary"]["status"] == "unverified"
    assert v["fields"][1]["problems"] == ["第 3 个片段（文字）出现数字“9”：数字请写成引用对象（日期用 WindowRef 或数据集里的日期）"]
    assert v["status"] == "failed" and not v["ok"] and v["unverified_fields"] == 1
    report["summary"] = ["华东成交额 ", HUADONG]
    assert ground_report(report, DATA, WINDOW)[1]["status"] == "all_passed"
    report["claims"].append({"type": "inference", "parts": ["可能来自促销"]})
    assert ground_report(report, DATA, WINDOW)[1]["status"] == "with_inference"
    only = {**report, "claims": [{"type": "inference", "parts": ["可能来自促销"]}]}
    status = ground_report(only, DATA, WINDOW)[1]
    assert status["status"] == "no_facts" and status["ok"]  # 只有推断：不显示“全部通过”


def test_incomplete_report_is_kept_as_is():
    report = {"title": "分析未完成", "summary": "分析停止（预算 3 次已用完）", "claims": [], "unexplained": "全部",
              "incomplete": True}
    rendered, v = ground_report(report, DATA, WINDOW)
    assert rendered == report and v["status"] == "no_facts" and v["claims"] == []
