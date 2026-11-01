"""分析用的确定性计算（ADR 0007）：数值逐项手算可核对。报告核验见 test_analysis_grounding.py（ADR 0009）。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from avelis.analysis import calc


def test_contribution_ranks_members_by_change_and_shares_sum_to_one():
    rows = [
        {"region": "华东", "cur": "500", "prev": "400"},
        {"region": "华南", "cur": "200", "prev": "350"},
        {"region": "华北", "cur": "300", "prev": "300"},
        {"region": "西南", "cur": "120", "prev": None},
    ]
    out = calc.contribution(rows, ["region"], "cur", "prev")
    assert out["total"] == {"previous": Decimal(1050), "current": Decimal(1120), "change": None, "change_rate": None}
    assert [r["region"] for r in out["rows"]] == ["华南", "华东", "华北", "西南"]
    south = out["rows"][0]
    assert south["change"] == Decimal(-150) and south["change_rate"] == Decimal("-0.4286")
    assert south["share_of_change"] is None  # 存在真实空值时总变化未知，不能计算贡献占比。
    assert out["rows"][-1]["change_rate"] is None and out["rows"][-1]["change"] is None
    assert sum(r["change"] for r in out["rows"] if r["change"] is not None) == Decimal(-50)
    with pytest.raises(calc.CalcError, match="缺少列"):
        calc.contribution(rows, ["city"], "cur", "prev")


def test_factor_decompose_lmdi_contributions_add_up_exactly():
    # 成交额 = 订单数 × 客单价：5000 = 100 × 50 → 4400 = 80 × 55
    out = calc.factor_decompose(("5000", "4400"), {"订单数": (100, 80), "客单价": (50, 55)})
    parts = {f["factor"]: f for f in out["factors"]}
    assert out["total"]["change"] == Decimal(-600)
    assert float(parts["订单数"]["contribution"]) == pytest.approx(-1047.348819851574)
    assert abs(sum(f["contribution"] for f in out["factors"]) - Decimal(-600)) < Decimal("1e-10")
    assert parts["客单价"]["change_rate"] == Decimal("0.1000")
    with pytest.raises(calc.CalcError, match="不一致"):
        calc.factor_decompose(("5000", "4400"), {"订单数": (100, 80), "客单价": (50, 60)})
    with pytest.raises(calc.CalcError, match="正数"):
        calc.factor_decompose(("5000", "0"), {"订单数": (100, 0)})


def test_calendar_adjust_separates_day_count_effect():
    out = calc.calendar_adjust(
        "3000",
        "3100",
        (date(2026, 9, 1), date(2026, 9, 30)),
        (date(2026, 8, 1), date(2026, 8, 31)),
        holidays={date(2026, 9, 25)},
    )
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
    ranked = calc.correlate([{"x": i, "y": i**3} for i in range(1, 11)], "x", "y")
    assert ranked["spearman"] == 1.0 and ranked["pearson"] < 1.0  # 单调但非线性


def test_segment_describe_and_outliers():
    rows = [{"d": "核心", "v": 10}, {"d": "核心", "v": 14}, {"d": "社区", "v": 5}, {"d": "社区", "v": 7}]
    seg = calc.segment_compare(rows, "d", ["v"])
    assert seg == [
        {"d": "核心", "count": 2, "v_sum": 24.0, "v_mean": 12.0, "v_median": 12.0},
        {"d": "社区", "count": 2, "v_sum": 12.0, "v_mean": 6.0, "v_median": 6.0},
    ]
    data = [{"s": f"S{i}", "v": v} for i, v in enumerate([10, 11, 12, 11, 10, 12, 11, 40])]
    out = calc.outliers(data, "v", ["s"])
    assert [r["s"] for r in out["rows"]] == ["S7"] and out["rows"][0]["side"] == "高"
    assert calc.outliers(data[:3], "v", ["s"])["rows"] == []


def test_coverage_counts_rows_and_missing_days():
    rows = [
        {"store": "A", "period": "2026-09-27"},
        {"store": "A", "period": "2026-09-28"},
        {"store": "B", "period": "2026-09-27"},
    ]
    out = calc.coverage(rows, ["store"], "period", date(2026, 9, 27), date(2026, 9, 28))
    assert out["rows"] == [
        {"store": "A", "rows": 2, "periods": 2, "missing_count": 0, "missing_periods": ""},
        {"store": "B", "rows": 1, "periods": 1, "missing_count": 1, "missing_periods": "2026-09-28"},
    ]
    assert out["summary"] == {
        "members": 2,
        "rows": 3,
        "expected_periods": 2,
        "members_with_gaps": 1,
        "members_complete": 1,
        "missing_total": 1,
    }
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
