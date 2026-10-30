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
    "D7": {"kind": "governed", "columns": ["客单价", "转化率", "订单数"], "rows": [{"客单价": "596.42514", "转化率": "0.3125",
                                                                         "订单数": "12897"}],
           "units": {"客单价": {"kind": "ratio", "unit": "元"}, "转化率": {"kind": "ratio"},
                     "订单数": {"kind": "level", "unit": "单"}}},
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


def test_display_is_decided_by_kind_not_by_the_model():
    """第 7 点第 1 条：系统按量纲决定显示格式与单位，模型只能选小数位（precision）。"""
    huadong = cell(match={"区域": "华东"})
    assert text(huadong) == "196.94万元"  # level、有单位：≥ 1 万用“万”，2 位
    assert text({**huadong, "precision": 0}) == "197万元" and text({**huadong, "precision": 4}) == "196.9398万元"
    assert text(cell(match={"区域": "西南"})) == "10元"  # 不到 1 万：原值
    assert text({"dataset": "D4", "column": "成交额"}) == "1.25万元"  # 列本身是万元：先按 unit_scale 换算回元
    assert text({"dataset": "D7", "column": "订单数"}) == "1.29万单"
    assert text(cell("成交额·变化率", match={"区域": "华南"})) == "+20.0%"  # rate_change：×100 带 %，没写方向带符号
    assert text({"dataset": "D2", "summary": "total.share"}) == "88.2%"  # share
    assert text({"dataset": "D7", "column": "转化率"}) == "31.3%"  # 无单位的比率（转化率）：百分比
    assert text({"dataset": "D7", "column": "客单价"}) == "596.43元"  # 有单位的比率：原值加单位
    assert text({"dataset": "D4", "column": "转化率·变化"}) == "-1.2个百分点"  # 比率之差
    assert text({"dataset": "D6", "column": "客流"}) == "1,200"  # unknown：原值、千分位、不带单位
    assert text({"dataset": "D5", "summary": "value"}, ) == "0.5"
    assert "结构不合法" in errors({**huadong, "format": "wan1"})[0]  # 模型不再选格式
    assert "结构不合法" in errors({**huadong, "precision": 5})[0]


def test_direction_is_checked_and_shown_as_absolute_value():
    down = cell("成交额·变化", match={"区域": "华东"}, direction="down")
    assert text("下降 ", down) == "下降 3.06万元"
    assert text(cell("成交额·变化", match={"区域": "华东"})) == "-3.06万元"  # 没写方向：带符号
    assert errors(cell("成交额·变化", match={"区域": "华东"}, direction="up")) == [
        "第 1 个片段（D1.成交额·变化[区域=华东]）：方向与正负不一致：direction=up，原值 -30601.95"]
    assert "水平值不能标方向" in errors(cell(match={"区域": "华东"}, direction="down"))[0]  # level
    assert "水平值不能标方向" in errors({"dataset": "D7", "column": "转化率", "direction": "up"})[0]  # ratio
    assert text({"dataset": "D2", "summary": "total.change", "direction": "down"}) == "1675单"
    assert text({"dataset": "D5", "summary": "value", "direction": "up"}) == "0.5"  # unknown 也可以写方向
    assert text({"dataset": "D2", "summary": "total.share", "direction": "up"}) == "88.2%"


def test_row_selection_and_summary_paths():
    assert text(cell(match={"区域": "华南"})) == "30.01万元"
    assert text(cell(row=2, precision=0)) == "30万元"
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
    assert "结构不合法" in errors(cell(row=1, precision=9))[0]
    assert "结构不合法" in errors({"window": "current"})[0]
    assert isinstance(NumberRef.model_validate(cell(row=1)), NumberRef)
    assert isinstance(LabelRef.model_validate({"dataset": "D1", "label": "门店", "row": 1}), LabelRef)
    assert CountRef.model_validate({"dataset": "D1", "distinct": "门店"}).distinct == "门店"
    assert WindowRef.model_validate({"window": "run_current"}).format == "range"


def check(parts, kind="fact", refs=None, **extra):
    report = {"title": "标题", "summary": "摘要", "unexplained": None, "conclusion": "insufficient_evidence",
              "claims": [{"type": kind, "parts": parts, "refs": refs or [], **extra}]}
    rendered, verification = ground_report(report, DATA, WINDOW)
    return rendered["claims"][0], verification["claims"][0], verification


HUADONG = cell(match={"区域": "华东"})


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


def test_rule4_only_chinese_quantity_expressions_are_rejected():
    """第 7 点第 2 条：只拦数量表达（数字后接量词或单位、几分之几、财务大写），不维护白名单与数量词表。"""
    for words, found in (("共三家门店", "三家"), ("两家门店", "两家"), ("前五家门店", "五家"), ("两个地区", "两个"),
                         ("营收两万元", "两万元"), ("十一个品类", "十一个"), ("打八折", "八折"), ("约五成", "五成"),
                         ("占百分之十", "百分之"), ("约三分之一", "三分之"), ("壹佰万", "壹")):
        _, result, _ = check([words, "，", HUADONG])
        assert result["status"] == "unverified", words
        assert f"出现中文数量“{found}”" in result["problems"][0], (words, result["problems"])
    assert check([HUADONG, "折"])[1]["status"] == "unverified"  # 数值段后面紧跟“折”仍拒绝（第 2 条）
    # 第 5 步评测里被误伤的说法：没有量词、序数、时间词、“一”
    for words in ("两期天数差，", "两渠道方向相反，", "九月下旬节假日，", "下一级类目与，", "只返回一条月度记录，",
                  "两期相比，", "两期合计，", "两者之差，", "口径统一、结果一致，是原因之一，", "十分明显，", "第二位，",
                  "促销折扣带动销量，", "出现转折，", "增长一倍，", "翻番，", "接近半数，", "不支持减半的说法，"):
        assert check([words, HUADONG])[1]["status"] == "verified", words


def test_rule5_refs_are_union_and_must_exist():
    claim, result, _ = check(["华东 ", HUADONG], refs=["D2"])
    assert claim["refs"] == ["D2", "D1"] and claim["explicit_refs"] == ["D2"] and result["status"] == "verified"
    assert check(["福田店关店"])[1]["problems"] == ["没有引用数据集：事实与计算类结论必须引用（显式 refs 或引用片段）"]
    _, missing, _ = check(["福田店关店"], refs=["D9"])
    assert missing["problems"] == ["引用了不存在的数据集 D9"] and missing["missing_refs"] == ["D9"]


def test_proposal_and_assumption_only_in_inference_or_recommendation():
    target = cell(match={"区域": "华东"}, role="proposal")
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
    assert v["sources"] == {"direct": {"governed": 1, "explore": 1, "calc": 0, "context": 0}, "with_explore": 1,
                            "assumption": 0, "windows": 0, "labels": 0, "failed": 0}


def test_inference_and_details_allow_numbers_and_still_render():
    claim, result, v = check(["可能约 3 成来自促销，", {"dataset": "D1", "label": "门店", "row": 2}], kind="inference",
                             premise=["促销力度与上期相近（约 ", cell("成交额·变化率", row=2), "）"])
    assert result["status"] == "unchecked" and result["problems"] == [] and v["ok"]
    assert claim["text"] == "可能约 3 成来自促销，深圳福田中心店" and claim["premise"] == "促销力度与上期相近（约 +20.0%）"
    assert claim["details"]["premise"]["status"] == "unchecked" and claim["details"]["premise"]["segments"][1]["kind"] == "value"
    assert "没有列 不存在" in check(["可能 ", cell("不存在", row=1)], kind="inference")[1]["problems"][0]


def test_fields_records_and_overall_status():
    report = {"title": [{"window": "run_current", "format": "month"}, "华东成交额下降"], "conclusion": "insufficient_evidence",
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
