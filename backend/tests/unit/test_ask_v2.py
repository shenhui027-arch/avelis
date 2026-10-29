"""问数表达能力 v2（不连库）：派生指标公式、对比期、演示规则对多指标 / 排名 / 对比的理解。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from avelis.ask.intent import DemoIntentModel, Intent, MetricCard, comparison_range
from avelis.governance.derived import evaluate, parse_formula
from avelis.governance.errors import Invalid

TODAY = date(2026, 10, 29)
REGION = [{"name": "region", "column": "region", "label": "区域"}]
CARDS = [
    MetricCard(code="paid_sales", name="实付销售额", aliases=[], dimensions=REGION,
               dimension_values={"region": ["华东", "华北", "华南"]}),
    MetricCard(code="orders_cnt", name="订单数", aliases=[], dimensions=REGION,
               dimension_values={"region": ["华东", "华北", "华南"]}),
    MetricCard(code="net_sales", name="退款后净销售额", aliases=["净销售额"], dimensions=REGION),
    MetricCard(code="aov", name="客单价", aliases=[], dimensions=REGION),
    MetricCard(code="category_sales", name="类目销售额", aliases=[],
               dimensions=[{"name": "category", "column": "c.category_name", "label": "类目"}]),
]


def parse(text: str) -> Intent:
    return DemoIntentModel().parse(text, CARDS, today=TODAY)


def test_formula_whitelist_and_codes():
    assert parse_formula("paid_sales / orders_cnt") == ["paid_sales", "orders_cnt"]
    assert parse_formula("(a - b) / a * 100") == ["a", "b"]
    for bad in ("__import__('os')", "a ** 2", "a.b", "a if b else c", "'x'", "a // b", "", "1 + 2"):
        with pytest.raises(Invalid):
            parse_formula(bad)


def test_formula_evaluation_is_null_safe():
    assert evaluate("paid_sales / orders_cnt", {"paid_sales": "300.00", "orders_cnt": 2}) == Decimal("150.0000")
    assert evaluate("a / b", {"a": 1, "b": 0}) is None  # 除数为 0：空，而不是报错或 0
    assert evaluate("a - b", {"a": 1, "b": None}) is None  # 任一组成值缺失：空
    assert evaluate("-a + 1", {"a": "2.5"}) == Decimal("-1.5000")


def test_comparison_ranges():
    assert comparison_range(date(2026, 9, 16), date(2026, 9, 16), "previous_period") == (date(2026, 9, 15),
                                                                                         date(2026, 9, 15))
    assert comparison_range(date(2026, 9, 1), date(2026, 9, 30), "previous_period") == (date(2026, 8, 2),
                                                                                        date(2026, 8, 31))
    assert comparison_range(date(2028, 2, 29), date(2028, 2, 29), "previous_year") == (date(2027, 2, 28),
                                                                                      date(2027, 2, 28))


def test_demo_rules_multi_metric_ranking_and_compare():
    multi = parse("2026-09-15 各区域实付销售额和订单数")
    assert multi.metric_codes == ["paid_sales", "orders_cnt"] and multi.group_by == ["region"]
    top = parse("2026-09-15 各区域实付销售额前 2")
    assert (top.order, top.limit, top.group_by) == ("desc", 2, ["region"])
    mom = parse("2026-09-16 华东区实付销售额 环比")
    assert mom.compare == "previous_period" and mom.filters[0].values == ["华东"]
    assert parse("2026-09-15 华东区实付销售额 同比").compare == "previous_year"
    # “退款后净销售额”包含“净销售额”别名：只算一个指标；“退款后”的“后”不会被当成“后 N 名”
    net = parse("2026-09-15 退款后净销售额")
    assert net.metric_codes == ["net_sales"] and net.order is None
    # 多个指标只能用共有维度：类目销售额没有区域维度
    mixed = parse("2026-09-15 华东区实付销售额和类目销售额")
    assert mixed.metric_codes == ["paid_sales", "category_sales"] and mixed.filters == []
    assert parse("2026-09-15 实付销售额排名").clarification  # 排名需要维度
