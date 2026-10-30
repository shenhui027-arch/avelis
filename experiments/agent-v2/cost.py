"""deepseek-flash 费用估算与 E1 运行账本，价格单位为元 / 百万 token。

北京时间工作日 9:00–12:00、14:00–18:00 按高峰价格，其余按空闲价格。
不识别法定节假日，工作日一律按高峰规则保守估算；推理 token 已包含在输出里，不重复计费。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

BEIJING = timezone(timedelta(hours=8))
RATES = {
    "peak": (Decimal("0.04"), Decimal("2"), Decimal("8")),
    "off_peak": (Decimal("0.02"), Decimal("1"), Decimal("4")),
}
MILLION = Decimal("1000000")


def price_period(dt: datetime) -> Literal["peak", "off_peak"]:
    """将带时区的时间转为北京时间，按工作日和小时判断价格时段。"""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("计费时间必须带时区")
    local = dt.astimezone(BEIJING)
    if local.weekday() < 5 and (9 <= local.hour < 12 or 14 <= local.hour < 18):
        return "peak"
    return "off_peak"


def request_cost(usage: dict, at: datetime) -> Decimal:
    """按一次模型步骤的输入、缓存命中和输出 token 估算费用。"""
    hit_rate, miss_rate, output_rate = RATES[price_period(at)]
    input_tokens = usage.get("input_tokens") or 0
    cache_hit_tokens = usage.get("cache_hit_tokens") or 0
    output_tokens = usage.get("output_tokens") or 0
    return (cache_hit_tokens * hit_rate + (input_tokens - cache_hit_tokens) * miss_rate
            + output_tokens * output_rate) / MILLION


def run_cost(run: dict) -> dict:
    """汇总所有 model 步骤（含失败步骤）；没有用量的步骤按零计并单独计数。"""
    tokens = dict.fromkeys(("input_tokens", "cache_hit_tokens", "cache_miss_tokens", "output_tokens"), 0)
    costs = {"peak": Decimal("0"), "off_peak": Decimal("0")}
    missing_usage = 0
    for step in run.get("steps") or []:
        if step.get("kind") != "model":
            continue
        usage = step.get("output") or {}
        if usage.get("input_tokens") is None and usage.get("output_tokens") is None:
            missing_usage += 1
            continue
        input_tokens = usage.get("input_tokens") or 0
        cache_hit_tokens = usage.get("cache_hit_tokens") or 0
        tokens["input_tokens"] += input_tokens
        tokens["cache_hit_tokens"] += cache_hit_tokens
        tokens["cache_miss_tokens"] += input_tokens - cache_hit_tokens
        tokens["output_tokens"] += usage.get("output_tokens") or 0
        at = datetime.fromisoformat(step["at"])
        costs[price_period(at)] += request_cost(usage, at)
    return {**tokens, "missing_usage": missing_usage,
            "peak_cny": str(costs["peak"]), "off_peak_cny": str(costs["off_peak"]),
            "total_cny": str(sum(costs.values(), Decimal("0")))}


def _amount(value) -> Decimal:
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("账本金额必须是非负有限数")
    return amount


class Ledger:
    """用已花金额与单次预留判断下一次运行是否可以开始。"""

    def __init__(self, cap_cny, spent_cny=0, *, per_run_reserve_cny):
        self.cap_cny = _amount(cap_cny)
        self.spent_cny = _amount(spent_cny)
        self.per_run_reserve_cny = _amount(per_run_reserve_cny)

    def can_start(self) -> bool:
        return self.spent_cny + self.per_run_reserve_cny <= self.cap_cny

    def add(self, cost) -> None:
        self.spent_cny += _amount(cost)

    def view(self) -> dict:
        return {"cap_cny": str(self.cap_cny), "spent_cny": str(self.spent_cny),
                "per_run_reserve_cny": str(self.per_run_reserve_cny),
                "remaining_cny": str(self.cap_cny - self.spent_cny), "can_start": self.can_start()}
