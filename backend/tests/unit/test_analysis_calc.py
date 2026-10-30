"""分析用的确定性计算与报告数字核验（ADR 0007）：数值逐项手算可核对。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from avelis.analysis import calc
from avelis.analysis.verify import extract_numbers, verify_report

CN_ERROR = "中文数字：请用阿拉伯数字书写"


def test_contribution_ranks_members_by_change_and_shares_sum_to_one():
    rows = [{"region": "华东", "cur": "500", "prev": "400"}, {"region": "华南", "cur": "200", "prev": "350"},
            {"region": "华北", "cur": "300", "prev": "300"}, {"region": "西南", "cur": "120", "prev": None}]
    out = calc.contribution(rows, ["region"], "cur", "prev")
    assert out["total"] == {"previous": Decimal(1050), "current": Decimal(1120), "change": Decimal(70),
                            "change_rate": Decimal("0.0667")}
    assert [r["region"] for r in out["rows"]] == ["华南", "西南", "华东", "华北"]
    south = out["rows"][0]
    assert south["change"] == Decimal(-150) and south["change_rate"] == Decimal("-0.4286")
    assert south["share_of_change"] == Decimal("-2.1429")  # 华南单独拉低的幅度超过总变化
    assert out["rows"][1]["change_rate"] is None  # 上期为 0（缺失）：变化率无意义，不给
    assert sum(r["change"] for r in out["rows"]) == out["total"]["change"]
    with pytest.raises(calc.CalcError, match="缺少列"):
        calc.contribution(rows, ["city"], "cur", "prev")


def test_factor_decompose_lmdi_contributions_add_up_exactly():
    # 成交额 = 订单数 × 客单价：5000 = 100 × 50 → 4400 = 80 × 55
    out = calc.factor_decompose(("5000", "4400"), {"订单数": (100, 80), "客单价": (50, 55)})
    parts = {f["factor"]: f for f in out["factors"]}
    assert out["total"]["change"] == Decimal(-600)
    assert parts["订单数"]["contribution"] == Decimal("-1047.35")  # L(4400, 5000) × ln(0.8) = 4693.64 × -0.22314
    assert sum(f["contribution"] for f in out["factors"]) == Decimal(-600)  # 正好等于总差额
    assert parts["客单价"]["change_rate"] == Decimal("0.1000")
    with pytest.raises(calc.CalcError, match="不一致"):
        calc.factor_decompose(("5000", "4400"), {"订单数": (100, 80), "客单价": (50, 60)})
    with pytest.raises(calc.CalcError, match="正数"):
        calc.factor_decompose(("5000", "0"), {"订单数": (100, 0)})


def test_calendar_adjust_separates_day_count_effect():
    out = calc.calendar_adjust("3000", "3100", (date(2026, 9, 1), date(2026, 9, 30)),
                               (date(2026, 8, 1), date(2026, 8, 31)), holidays={date(2026, 9, 25)})
    assert out["current"]["days"] == 30 and out["previous"]["days"] == 31
    assert out["current"]["weekend_days"] == 8 and out["previous"]["weekend_days"] == 10
    assert out["current"]["holidays"] == 1
    assert out["current"]["daily"] == Decimal("100.00") and out["previous"]["daily"] == Decimal("100.00")
    assert out["day_count_effect"] == Decimal("-100.00") and out["daily_level_effect"] == Decimal("0.00")
    assert out["daily_change_rate"] == Decimal("0.0000")  # 少一天造成的下降，日均没有变化


def test_correlate_reports_sample_size_and_p_value():
    rows = [{"x": i, "y": 2 * i + 1} for i in range(10)]
    perfect = calc.correlate(rows, "x", "y")
    assert perfect["n"] == 10 and perfect["pearson"] == 1.0 and perfect["p_value"] == 0.0
    # r = 0.5、n = 20：t = 2.449，自由度 18，双侧 p ≈ 0.0248
    assert calc._p_value(0.5, 20) == pytest.approx(0.0248, abs=2e-4)
    assert calc._p_value(0.3, 10) == pytest.approx(0.3995, abs=2e-3)
    few = calc.correlate(rows[:5], "x", "y")
    assert "pearson" not in few and "少于" in few["note"]
    flat = calc.correlate([{"x": i, "y": 3} for i in range(10)], "x", "y")
    assert "pearson" not in flat and "没有变化" in flat["note"]
    ranked = calc.correlate([{"x": i, "y": i ** 3} for i in range(1, 11)], "x", "y")
    assert ranked["spearman"] == 1.0 and ranked["pearson"] < 1.0  # 单调但非线性


def test_segment_describe_and_outliers():
    rows = [{"d": "核心", "v": 10}, {"d": "核心", "v": 14}, {"d": "社区", "v": 5}, {"d": "社区", "v": 7}]
    seg = calc.segment_compare(rows, "d", ["v"])
    assert seg == [{"d": "核心", "count": 2, "v_sum": 24.0, "v_mean": 12.0, "v_median": 12.0},
                   {"d": "社区", "count": 2, "v_sum": 12.0, "v_mean": 6.0, "v_median": 6.0}]
    data = [{"s": f"S{i}", "v": v} for i, v in enumerate([10, 11, 12, 11, 10, 12, 11, 40])]
    out = calc.outliers(data, "v", ["s"])
    assert [r["s"] for r in out["rows"]] == ["S7"] and out["rows"][0]["side"] == "高"
    assert calc.outliers(data[:3], "v", ["s"])["rows"] == []


def test_extract_numbers_skips_dates_and_ordinals():
    text = "2026 年 9 月华南门店成交额 196.94 万，比 8 月下降 7,625.24（-3.7%），前 3 名中第 1 是 D2 的深圳"
    assert [n["text"] for n in extract_numbers(text)] == ["196.94万", "7,625.24", "-3.7%"]
    # 天数是日历折算数据集里的数：按普通数字核验（第五轮）
    assert [n["text"] for n in extract_numbers("9 月 10 日成交额 15,418.64，9 月 1 日至 9 月 9 日共 9 天")] == ["15,418.64", "9"]
    assert [n["text"] for n in extract_numbers("2026-08-01~2026-08-09 订单 168")] == ["168"]
    # 短写法不当日期：9/10、08-09、2026-09 按数字核验（复核：只认完整、真实存在的日期）
    assert [n["text"] for n in extract_numbers("9/10 成交额 15,418.64")] == ["9", "10", "15,418.64"]
    assert [n["text"] for n in extract_numbers("08-09 订单 168")] == ["08", "09", "168"]
    assert [n["text"] for n in extract_numbers("8-9 月共 61 天")] == ["61"]  # 超过 31 的天数是数据，要核验
    assert [n["text"] for n in extract_numbers("9 月少 1 天，8 月 31 天，其余 9 家门店")] == ["1", "31", "9"]
    # 数字紧贴汉字（\w 在 Unicode 下包括汉字，曾导致只取到千分位逗号后面的部分）
    assert [n["text"] for n in extract_numbers("9月订单数10,998单，减少1,899单，变化率-14.72%")] ==         ["10,998", "1,899", "-14.72%"]


def test_verify_report_checks_fact_numbers_against_cited_datasets():
    datasets = {"D1": {"rows": [{"region": "华南", "current": "1969398.05", "previous": "1977023.29",
                                 "change": "-7625.24", "change_rate": "-0.0039"}]},
                "D2": {"rows": [], "summary": {"total": {"change": "-600"}}}}
    claims = [
        {"type": "fact", "text": "华南 9 月门店成交额 196.94 万，下降 7,625.24（-0.39%）", "refs": ["D1"]},
        {"type": "calc", "text": "量价拆解：总差额 -600", "refs": ["D2"]},
        {"type": "fact", "text": "华南下降 12.5%", "refs": ["D1"]},  # 数据集中没有这个数
        {"type": "fact", "text": "订单数 3119", "refs": ["D9"]},  # 引用了不存在的数据集
        {"type": "inference", "text": "可能与关店有关，约影响 30%", "refs": []},  # 推断不强制核验
    ]
    out = verify_report(claims, datasets)
    status = [c["status"] for c in out["claims"]]
    assert status == ["verified", "verified", "unverified", "unverified", "unchecked"]
    assert out["claims"][2]["numbers"] == [{"text": "12.5%", "verified": False}]
    assert out["claims"][3]["missing_refs"] == ["D9"]
    assert not out["ok"] and out["unverified"] == 2  # 按结论计：两条事实类结论未核实
    assert verify_report(claims[:2], datasets)["ok"]
    # 计数：数据集的行数与列的不同值个数可以核实；自己推算的计数不行
    stores = {"D3": {"rows": [{"store": f"S{i}", "v": i * 100} for i in range(10)]}}
    counted = verify_report([{"type": "fact", "text": "华南共 10 家门店", "refs": ["D3"]},
                             {"type": "fact", "text": "其余 9 家门店", "refs": ["D3"]}], stores)
    assert [c["status"] for c in counted["claims"]] == ["verified", "unverified"]


def test_coverage_counts_rows_and_missing_days():
    rows = [{"store": "A", "period": "2026-09-27"}, {"store": "A", "period": "2026-09-28"},
            {"store": "B", "period": "2026-09-27"}]
    out = calc.coverage(rows, ["store"], "period", date(2026, 9, 27), date(2026, 9, 28))
    assert out["rows"] == [
        {"store": "A", "rows": 2, "periods": 2, "missing_count": 0, "missing_periods": ""},
        {"store": "B", "rows": 1, "periods": 1, "missing_count": 1, "missing_periods": "2026-09-28"}]
    assert out["summary"] == {"members": 2, "rows": 3, "expected_periods": 2, "members_with_gaps": 1,
                              "members_complete": 1, "missing_total": 1}
    assert calc.coverage(rows, [], None)["summary"]["rows"] == 3


def test_factor_decompose_keeps_input_inconsistency_out_of_factors():
    """总量与因子之积不一致（口径、精度）时，残差单列为“未解释差额”，不并入最后一个因子（审计 #7）。"""
    out = calc.factor_decompose(("1000", "1004"), {"订单数": (1000, 1000), "客单价": (1, 1)})
    rows = {f["factor"]: f["contribution"] for f in out["factors"]}
    assert rows == {"订单数": Decimal("0.00"), "客单价": Decimal("0.00"), "未解释差额": Decimal("4.00")}
    assert out["unexplained"] == Decimal("4.00")
    assert sum(rows.values()) == out["total"]["change"]
    # 一致的输入只有舍入尾差，不产生“未解释差额”
    consistent = calc.factor_decompose(("5000", "4400"), {"订单数": (100, 80), "客单价": (50, 55)})
    assert [f["factor"] for f in consistent["factors"]] == ["订单数", "客单价"] and consistent["unexplained"] == 0


def test_verify_sign_must_match_unless_decrease_wording():
    """去掉全局绝对值匹配：只有“下降 / 减少 / 负贡献”后面不带符号的数字才能对应负值（审计 #6）。"""
    ds = {"D1": {"rows": [{"change": "-7625.24", "rate": "-0.037"}]}}

    def status(text):
        return verify_report([{"type": "fact", "text": text, "refs": ["D1"]}], ds)["claims"][0]["status"]

    assert status("成交额下降 7,625.24") == "verified"
    assert status("成交额减少了 7,625.24，降幅 3.7%") == "verified"
    assert status("成交额变化 -7,625.24（-3.7%）") == "verified"
    assert status("成交额增长 7,625.24") == "unverified"  # 方向写反
    assert status("成交额变化 7,625.24") == "unverified"  # 没有表示减少的词，正数不能对应负值
    assert status("成交额变化 +3.7%") == "unverified"
    assert status("成交额下降到 7,625.24") == "unverified"  # “下降到”是水平，不是幅度


def test_verify_requires_refs_ranges_and_parseable_numbers():
    """没有引用的事实 / 计算、区间端点、大写数字（审计 #5）。"""
    ds = {"D1": {"rows": [{"store": "A", "v": "2400"}, {"store": "B", "v": "3000"}, {"store": "C", "v": "81000"}]}}

    def check(text, refs=("D1",), kind="fact"):
        return verify_report([{"type": kind, "text": text, "refs": list(refs)}], ds)["claims"][0]

    no_refs = check("福田店关店", refs=())
    assert no_refs["status"] == "unverified" and no_refs["no_refs"]
    assert check("差额拆解", refs=(), kind="calc")["status"] == "unverified"
    assert check("日均约 2400–3200 元/天")["numbers"] == [{"text": "2400", "verified": True},
                                                       {"text": "3200", "verified": False}]
    assert check("日均 2400–3000 元/天")["status"] == "verified"
    assert check("约 8-9 万元")["numbers"] == [{"text": "8万", "verified": True}, {"text": "9万", "verified": False}]
    assert check("成交额壹佰万元")["numbers"] == [{"text": "壹佰万", "verified": False, "error": "无法解析的数字"}]
    # 中文数字表示的数量不能可靠解析：一律未核实（系统提示要求用阿拉伯数字）
    for text in ("共三家门店", "营收三点五万元", "共五个区域", "百分之十二的订单"):
        assert check(text)["status"] == "unverified", text
    assert check("营收三点五万元")["numbers"] == [{"text": "三点五万", "verified": False, "error": CN_ERROR}]  # 不会被当成 5 万
    assert check("共 3 家门店")["status"] == "verified"
    assert check("统一口径，结果一致，是原因之一，第一名是福田店")["status"] == "verified"  # 不是数量
    # 以失败为默认（第四轮）：字母紧贴的数字照常提取；“零点五万”“〇个”未核实；还有认不出的数字字符时兜底为未核实
    assert [n["text"] for n in extract_numbers("GMV100万")] == ["100万"]
    assert check("GMV100万")["status"] == "unverified"  # D1 中没有 100 万
    assert check("营收零点五万")["numbers"] == [{"text": "零点五万", "verified": False, "error": CN_ERROR}]
    assert check("共〇个区域")["status"] == "unverified"
    leftover = check("持续五个月，两期对比")
    assert leftover["status"] == "unverified" and leftover["numbers"] and not leftover["numbers"][-1]["verified"]
    assert extract_numbers("销售额翻了两番")[0]["error"] == "包含无法识别的数字"  # 两番：认不出的数量
    # 验收反例：引用数据集里原样出现的名称（“广州四号店”）不是数量；“两家”、纯数字值“三”仍未核实
    stores = {"D1": {"rows": [{"store": "广州四号店", "delta": "25434.45"}, {"store": "厦门二号店", "delta": "44494.61"},
                              {"store": "三", "delta": "1"}, {"store": "2026-09", "delta": "2"}]}}
    named = "其余多数为增长（如广州四号店增加 25434.45、厦门二号店增加 44494.61）"
    assert verify_report([{"type": "fact", "text": named, "refs": ["D1"]}], stores)["ok"] is True
    assert verify_report([{"type": "fact", "text": named, "refs": ["D1"]}], {"D1": {"rows": [
        {"delta": "25434.45"}, {"delta": "44494.61"}]}})["ok"] is False  # 名称不在引用的数据集里：仍兜底未核实
    for text in ("广州四号店两家合计 25434.45", "广州五号店增加 25434.45", "共三家门店", "2026-09 增加 2"):
        assert verify_report([{"type": "fact", "text": text, "refs": ["D1"]}], stores)["ok"] is False, text
    assert verify_report([{"type": "fact", "text": "x", "refs": ["D1"]}], stores,
                         {"summary": "广州四号店增长最多"})["ok"] is True  # 摘要同样适用
    # ASCII 连字符的金额区间不是日期：两端都核验（复核反例：曾被当成 2400-32 删掉，只剩 0）
    zero = {"D1": {"rows": [{"v": "0"}]}}
    assert verify_report([{"type": "fact", "text": "利润在 2400-3200 元之间", "refs": ["D1"]}], zero)["ok"] is False
    assert check("利润在 2400-3000 元之间")["status"] == "verified"
    assert [n["text"] for n in extract_numbers("2026-09-15 各 100，2000-12 元")] == ["100", "2000", "12"]
    assert [n["text"] for n in extract_numbers("13-40 单，45 日")] == ["13", "40", "45"]  # 不合法的月日照常核验
    # 复核反例：单位不在白名单里的区间、“零个”、不存在的日期，都不能因为“没提取到数字”而通过
    assert [n["text"] for n in extract_numbers("销量10-12箱")] == ["10", "12"]
    assert check("共零个区域")["status"] == "unverified"
    assert "100" in [n["text"] for n in extract_numbers("2026-02-31 成交 100")]
    assert [n["text"] for n in extract_numbers("2026-02-31 成交 100")][0] == "2026"  # 不存在的日期按数字核验
    assert [n["text"] for n in extract_numbers("2026-02-29 成交 100")][0] == "2026"  # 平年没有 2 月 29 日
    assert [n["text"] for n in extract_numbers("2024-02-29 成交 100")] == ["100"]
    assert [n["text"] for n in extract_numbers("2 月 31 日成交 100")] == ["2", "31", "100"]
    assert check("销量10-12箱")["status"] == "unverified"


def test_comparison_and_calendar_wording_is_not_a_quantity():
    """第五轮：指代对比双方的固定说法与日期、期间写法不是数量；“两家门店”与日历折算的天数仍要核验。"""
    def texts(text):
        return [n["text"] for n in extract_numbers(text)]

    ds = {"D19": {"rows": [{"diff": "-1675.0000"}]}}
    claim = [{"type": "calc", "text": "华东线上订单数两期差额为 -1675 笔（D19）", "refs": ["D19"]}]
    assert texts(claim[0]["text"]) == ["-1675"]
    assert verify_report(claim, ds)["ok"] is True
    for text in ("两个时期对比", "两个周期合计", "两段相比", "两者之差"):
        assert texts(text) == [], text
    assert verify_report([{"type": "fact", "text": "两家门店下降 -1675", "refs": ["D19"]}], ds)["ok"] is False
    assert verify_report([{"type": "fact", "text": "两个区域", "refs": ["D19"]}], ds)["ok"] is False
    # 日期与期间写法
    for text in ("9月", "9 月", "9月份", "九月", "十二月份", "15日", "15 号", "9 月上旬", "第3季度", "Q3", "3 季度",
                 "三季度", "第2周", "上半年", "下半年", "2026年", "本期", "上期", "同期"):
        assert texts(text + "成交 100") == ["100"], text
    assert texts("广州10号店成交 100") == ["10", "100"]  # 店名里的“10 号”不是日期
    assert texts("45 日成交 100") == ["45", "100"]  # 不合法的日期照常核验
    # 日历折算的天数、周末日、节假日数：按普通数字核验，引用 calendar_adjust 的数据集即可通过
    cal = {"D20": {"rows": [{"period": "current", "days": 30, "weekend_days": 8, "holidays": 3, "daily": "240.60"},
                            {"period": "previous", "days": 31, "weekend_days": 10, "holidays": 0, "daily": "286.87"}]}}
    text = "日历折算：本期 30 天（8 个周末日、3 个节假日）、上期 31 天（10 个周末日、0 个节假日）；日均 240.60 笔对 286.87 笔"
    assert texts(text) == ["30", "8", "3", "31", "10", "0", "240.60", "286.87"]
    assert verify_report([{"type": "calc", "text": text, "refs": ["D20"]}], cal)["ok"] is True
    assert verify_report([{"type": "calc", "text": "本期 29 天", "refs": ["D20"]}], cal)["ok"] is False


def test_verify_checks_title_summary_and_unexplained():
    """摘要、标题与“未解释”中的数字同样核验，对照事实 / 计算类结论引用的数据集（审计 #4）。"""
    ds = {"D1": {"rows": [{"v": "1969398.05"}]}, "D2": {"rows": [{"v": "555"}]}}
    claims = [{"type": "fact", "text": "成交额 196.94 万", "refs": ["D1"]}]
    ok = verify_report(claims, ds, {"title": "9 月成交额 196.94 万", "summary": "成交额 196.94 万", "unexplained": None})
    assert ok["ok"] and ok["unverified_fields"] == 0
    bad = verify_report(claims, ds, {"title": "成交额分析", "summary": "下降约 30%，福田店少了 555 单"})
    assert not bad["ok"] and bad["unverified"] == 0 and bad["unverified_fields"] == 1
    summary = bad["fields"][1]
    assert summary["field"] == "summary" and summary["numbers"] == [
        {"text": "30%", "verified": False}, {"text": "555", "verified": False}]  # D2 没有被任何事实引用
