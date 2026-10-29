"""分析用的确定性计算与报告数字核验（ADR 0007）：数值逐项手算可核对。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from avelis.analysis import calc
from avelis.analysis.verify import extract_numbers, verify_report


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
    assert [n["text"] for n in extract_numbers("9/10 成交额 15,418.64，9/1–9/9 共 9 天")] == ["15,418.64", "9"]
    assert [n["text"] for n in extract_numbers("2026-08-01~08-09 订单 168")] == ["168"]
    assert [n["text"] for n in extract_numbers("8-9 月共 61 天")] == ["61"]
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
