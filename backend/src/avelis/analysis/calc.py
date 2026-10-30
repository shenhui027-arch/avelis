"""分析用的确定性计算（ADR 0007 第 3 点）：模型不心算，所有数字都由这里从数据集算出。

输入输出都是“行”（dict 列表），数值用 Decimal（金额）或 float（统计量），不依赖 numpy / scipy。
"""

from __future__ import annotations

import math
import statistics
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation  # noqa: F401 - Decimal 也供工具层使用
from typing import Any

Row = dict[str, Any]


class CalcError(ValueError):
    """输入不满足计算前提（缺列、非数值、非正数等）：如实报错，不猜测。"""


def number(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise CalcError(f"不是数值：{value!r}") from None


def _q(value: Decimal | None, places: int = 4) -> Decimal | None:
    return None if value is None else value.quantize(Decimal(1).scaleb(-places))


# --- 两期差额按维度拆解 ---------------------------------------------------------------

def contribution(rows: list[Row], keys: list[str], current: str, previous: str) -> dict[str, Any]:
    """每个维度成员的两期值、变化与占总变化的比例，按变化绝对值排序。缺失按 0（该成员当期没有发生）。"""
    for col in (*keys, current, previous):
        if rows and col not in rows[0]:
            raise CalcError(f"缺少列 {col}")
    merged: dict[tuple, list[Decimal]] = {}
    for r in rows:
        k = tuple(r[c] for c in keys)
        cur, prev = number(r[current]) or Decimal(0), number(r[previous]) or Decimal(0)
        acc = merged.setdefault(k, [Decimal(0), Decimal(0)])
        acc[0] += cur
        acc[1] += prev
    total_cur = sum((v[0] for v in merged.values()), Decimal(0))
    total_prev = sum((v[1] for v in merged.values()), Decimal(0))
    total_change = total_cur - total_prev
    out = []
    for k, (cur, prev) in merged.items():
        change = cur - prev
        out.append({**dict(zip(keys, k)), "previous": prev, "current": cur, "change": change,
                    "change_rate": _q(change / prev) if prev else None,
                    "share_of_change": _q(change / total_change) if total_change else None})
    out.sort(key=lambda r: (-abs(r["change"]), str([r[c] for c in keys])))
    return {"rows": out, "total": {"previous": total_prev, "current": total_cur, "change": total_change,
                                   "change_rate": _q(total_change / total_prev) if total_prev else None}}


# --- 乘法关系的因子拆解（LMDI）------------------------------------------------------------

def _log_mean(a: float, b: float) -> float:
    return a if math.isclose(a, b, rel_tol=1e-12) else (a - b) / (math.log(a) - math.log(b))


def factor_decompose(total: tuple[Any, Any], factors: dict[str, tuple[Any, Any]]) -> dict[str, Any]:
    """总量 = 各因子之积（如 总量 = 数量 × 单价）。用对数平均分解（LMDI-I），各因子贡献相加等于总差额。
    total、factors 的值均为（上期, 本期）。要求全部为正数，且各因子之积与总量一致（误差 0.5% 以内）。
    各因子贡献相加等于因子之积的变化；总量与因子之积的差（输入不一致）单列为“未解释差额”，不归给任何因子。"""
    t0, t1 = (number(v) for v in total)
    if t0 is None or t1 is None or t0 <= 0 or t1 <= 0:
        raise CalcError("总量必须为正数")
    vals = {}
    for name, (a, b) in factors.items():
        a, b = number(a), number(b)
        if a is None or b is None or a <= 0 or b <= 0:
            raise CalcError(f"因子 {name} 必须为正数")
        vals[name] = (a, b)
    for i, label in ((0, "上期"), (1, "本期")):
        product = math.prod(float(v[i]) for v in vals.values())
        target = float((t0, t1)[i])
        if abs(product - target) > 0.005 * target:
            raise CalcError(f"{label}各因子之积 {product:.4f} 与总量 {target:.4f} 不一致")
    change = t1 - t0
    # 按因子自身的乘积计算总量的对数平均，贡献才只反映因子的变化：输入与总量的不一致不会被摊到某个因子上
    p0 = math.prod(float(v[0]) for v in vals.values())
    p1 = math.prod(float(v[1]) for v in vals.values())
    weight = _log_mean(p1, p0)
    raw = {name: weight * math.log(float(b) / float(a)) for name, (a, b) in vals.items()}
    names = list(raw)
    rounded = {n: Decimal(str(raw[n])).quantize(Decimal("0.01")) for n in names}
    residual = change - sum(rounded.values(), Decimal(0))
    rows = [{"factor": n, "previous": vals[n][0], "current": vals[n][1],
             "change_rate": _q((vals[n][1] - vals[n][0]) / vals[n][0]), "contribution": rounded[n]} for n in names]
    # 未舍入的贡献之和与总差额的差不超过总量最小单位的一半（按总量输入的小数位数）时，
    # 视为计算尾差，舍入尾差并入最后一个因子；否则差额来自输入（口径、显示精度）不一致，整体单列为“未解释差额”
    places = max(0, *(-v.as_tuple().exponent for v in (t0, t1) if isinstance(v.as_tuple().exponent, int)))
    tolerance = Decimal("0.5").scaleb(-places)
    if abs(change - Decimal(str(sum(raw.values())))) <= tolerance:
        rows[-1]["contribution"] += residual
    else:
        rows.append({"factor": "未解释差额", "previous": None, "current": None, "change_rate": None,
                     "contribution": residual})
    for r in rows:
        r["share"] = _q(r["contribution"] / change) if change else None
    return {"total": {"previous": t0, "current": t1, "change": change}, "factors": rows,
            "unexplained": residual if rows[-1]["factor"] == "未解释差额" else Decimal(0)}


# --- 日历折算 -----------------------------------------------------------------------

def period_days(start: date, end: date, holidays: set[date] | None = None) -> dict[str, int]:
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    holidays = holidays or set()
    return {"days": len(days), "weekend_days": sum(1 for d in days if d.weekday() >= 5),
            "holidays": sum(1 for d in days if d in holidays)}


def calendar_adjust(current: Any, previous: Any, current_period: tuple[date, date],
                    previous_period: tuple[date, date], holidays: set[date] | None = None) -> dict[str, Any]:
    """按天数折算：日均值的变化，以及天数差异本身带来的变化（两期天数、周末、节假日数一并给出）。"""
    cur, prev = number(current), number(previous)
    if cur is None or prev is None:
        raise CalcError("两期值都需要")
    dc, dp = period_days(*current_period, holidays), period_days(*previous_period, holidays)
    daily_cur, daily_prev = cur / dc["days"], prev / dp["days"]
    # 本期天数按上期日均值折算出的“天数效应”；其余为日均水平的变化
    day_effect = daily_prev * (dc["days"] - dp["days"])
    return {"current": {**dc, "value": cur, "daily": _q(daily_cur, 2)},
            "previous": {**dp, "value": prev, "daily": _q(daily_prev, 2)},
            "change": cur - prev, "day_count_effect": _q(day_effect, 2),
            "daily_level_effect": _q(cur - prev - day_effect, 2),
            "daily_change_rate": _q((daily_cur - daily_prev) / daily_prev) if daily_prev else None}


# --- 相关性 -------------------------------------------------------------------------

def _betacf(a: float, b: float, x: float) -> float:
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > 1e-300 else 1e-300)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d
        d = 1 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1 + aa / c if abs(1 + aa / c) > 1e-300 else 1e-300
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d
        d = 1 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1 + aa / c if abs(1 + aa / c) > 1e-300 else 1e-300
        delta = d * c
        h *= delta
        if abs(delta - 1) < 3e-14:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    """正则化不完全 Beta 函数 I_x(a, b)。"""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x))
    if x < (a + 1) / (a + b + 2):
        return front * _betacf(a, b, x) / a
    return 1 - front * _betacf(b, a, 1 - x) / b


def _p_value(r: float, n: int) -> float:
    """相关系数为 0 的双侧检验（t 分布，自由度 n - 2）。"""
    if abs(r) >= 1:
        return 0.0
    df = n - 2
    t = r * math.sqrt(df / (1 - r * r))
    return _betainc(df / 2, 0.5, df / (df + t * t))


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


MIN_SAMPLES = 8


def correlate(rows: list[Row], x: str, y: str) -> dict[str, Any]:
    """皮尔逊与斯皮尔曼相关系数、样本量与双侧 p 值。样本少于 8 个时只给提示，不给系数结论。缺失值成对剔除。"""
    pairs = [(float(number(r[x])), float(number(r[y]))) for r in rows
             if r.get(x) is not None and r.get(y) is not None]
    n = len(pairs)
    out: dict[str, Any] = {"x": x, "y": y, "n": n}
    if n < MIN_SAMPLES:
        return {**out, "note": f"样本只有 {n} 个，少于 {MIN_SAMPLES} 个，不计算相关系数"}
    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
    pearson = _pearson(xs, ys)
    spearman = _pearson(_ranks(xs), _ranks(ys))
    if pearson is None or spearman is None:
        return {**out, "note": "其中一列没有变化，相关系数无意义"}
    p = _p_value(pearson, n)
    strength = "强" if abs(pearson) >= 0.7 else ("中等" if abs(pearson) >= 0.4 else "弱")
    return {**out, "pearson": round(pearson, 4), "spearman": round(spearman, 4), "p_value": round(p, 6),
            "significant": p < 0.05,
            "note": f"{strength}{'正' if pearson > 0 else '负'}相关（p={p:.4f}）；相关不等于因果"}


# --- 覆盖度 -------------------------------------------------------------------------

def coverage(rows: list[Row], keys: list[str], period: str | None, start: date | None = None,
             end: date | None = None) -> dict[str, Any]:
    """各成员的行数与覆盖的日期；给定起止日期时列出缺失的日期（period 为 YYYY-MM-DD）。"""
    expected = None
    if period and start and end:
        expected = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    groups: dict[tuple, list[Row]] = {}
    for r in rows:
        groups.setdefault(tuple(r.get(k) for k in keys), []).append(r)
    out = []
    for key in sorted(groups, key=str):
        members = groups[key]
        seen = sorted({str(r[period]) for r in members}) if period else []
        missing = [d for d in expected if d not in set(seen)] if expected is not None else []
        out.append({**dict(zip(keys, key)), "rows": len(members), "periods": len(seen) if period else None,
                    "missing_count": len(missing) if expected is not None else None,
                    "missing_periods": ",".join(missing) if missing else ""})
    summary = {"members": len(groups), "rows": len(rows), "expected_periods": len(expected) if expected else None,
               "members_with_gaps": sum(1 for r in out if r["missing_count"]),
               "members_complete": sum(1 for r in out if r["missing_count"] == 0) if expected is not None else None,
               "missing_total": sum(r["missing_count"] or 0 for r in out)}
    return {"rows": out, "summary": summary}


# --- 分组对比、描述统计、异常值 -----------------------------------------------------------

def describe(values: list[Any]) -> dict[str, Any]:
    nums = [float(number(v)) for v in values if v is not None]
    if not nums:
        return {"count": 0}
    out = {"count": len(nums), "sum": round(sum(nums), 4), "mean": round(statistics.fmean(nums), 4),
           "median": round(statistics.median(nums), 4), "min": min(nums), "max": max(nums)}
    if len(nums) > 1:
        out["stdev"] = round(statistics.stdev(nums), 4)
    return out


def segment_compare(rows: list[Row], segment: str, measures: list[str]) -> list[Row]:
    """按分组列对比各度量：每组的个数、合计、均值、中位数。"""
    groups: dict[Any, list[Row]] = {}
    for r in rows:
        groups.setdefault(r.get(segment), []).append(r)
    out = []
    for key in sorted(groups, key=lambda k: (k is None, str(k))):
        item: Row = {segment: key, "count": len(groups[key])}
        for m in measures:
            d = describe([r.get(m) for r in groups[key]])
            item.update({f"{m}_sum": d.get("sum"), f"{m}_mean": d.get("mean"), f"{m}_median": d.get("median")})
        out.append(item)
    return out


def outliers(rows: list[Row], value: str, keys: list[str]) -> dict[str, Any]:
    """四分位距法（1.5 × IQR）找异常值。少于 4 个值时不判断。"""
    nums = sorted(float(number(r[value])) for r in rows if r.get(value) is not None)
    if len(nums) < 4:
        return {"note": "数据少于 4 个，不判断异常值", "rows": []}
    q1, _, q3 = statistics.quantiles(nums, n=4)
    low, high = q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)
    flagged = [{**{k: r.get(k) for k in keys}, value: r[value], "side": "高" if float(number(r[value])) > high else "低"}
               for r in rows if r.get(value) is not None and not low <= float(number(r[value])) <= high]
    return {"q1": round(q1, 4), "q3": round(q3, 4), "low": round(low, 4), "high": round(high, 4), "rows": flagged}
