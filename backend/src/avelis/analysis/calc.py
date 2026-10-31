"""分析用的确定性计算（ADR 0007 第 3 点）：模型不心算，所有数字都由这里从数据集算出。

输入输出都是“行”（dict 列表），数值用 Decimal（精确小数）或 float（统计量），不依赖 numpy / scipy。
"""

from __future__ import annotations

import math
import statistics
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, localcontext  # noqa: F401 - Decimal 也供工具层使用
from typing import Any

from avelis.governance.derived import significant12 as _significant12

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
    """每个维度成员的两期值与变化。某期没有行按 0，有行但值为空则保留未知。"""
    for col in (*keys, current, previous):
        if rows and not any(col in r for r in rows):
            raise CalcError(f"缺少列 {col}")
    merged: dict[tuple, list[Decimal]] = {}
    missing: dict[tuple, set[int]] = {}
    missing_values = 0
    for r in rows:
        k = tuple(r[c] for c in keys)
        acc = merged.setdefault(k, [Decimal(0), Decimal(0)])
        for i, column in enumerate((current, previous)):
            value = number(r.get(column, 0))
            if value is None:
                missing.setdefault(k, set()).add(i)
                missing_values += 1
            else:
                acc[i] += value
    total_cur = sum((v[0] for v in merged.values()), Decimal(0))
    total_prev = sum((v[1] for v in merged.values()), Decimal(0))
    total_change = None if missing_values else total_cur - total_prev
    out = []
    for k, (cur, prev) in merged.items():
        cur = None if 0 in missing.get(k, set()) else cur
        prev = None if 1 in missing.get(k, set()) else prev
        change = cur - prev if cur is not None and prev is not None else None
        out.append({**dict(zip(keys, k)), "previous": prev, "current": cur, "change": change,
                    "change_rate": _q(change / prev) if change is not None and prev else None,
                    "share_of_change": _q(change / total_change) if change is not None and total_change else None})
    out.sort(key=lambda r: (r["change"] is None, -abs(r["change"]) if r["change"] is not None else 0,
                            str([r[c] for c in keys])))
    return {"rows": out, "total": {"previous": total_prev, "current": total_cur, "change": total_change,
                                   "change_rate": _q(total_change / total_prev)
                                   if total_change is not None and total_prev else None},
            "summary": {"missing_values": missing_values,
                        "totals_note": "总量仅合计有值部分；两期值为已知部分合计，存在缺失值时总变化、变化率及成员贡献占比未知"
                        if missing_values else "总量包含全部已知值"}}


# --- 乘法关系的因子拆解（LMDI）------------------------------------------------------------

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
    # 因子之积与对数平均都用高精度 Decimal 计算：未解释差额 = 总变化 − 因子之积的变化，只来自输入不一致；
    # 各因子贡献之和在 Decimal 精度内等于因子之积的变化，浮点转换与对数的误差（随总量量级放大）不会进入残差
    with localcontext() as context:
        context.prec = 50
        p0 = math.prod((v[0] for v in vals.values()), start=Decimal(1))
        p1 = math.prod((v[1] for v in vals.values()), start=Decimal(1))
        weight = p1 if p1 == p0 else (p1 - p0) / (p1.ln() - p0.ln())
        exact = {name: weight * (b / a).ln() for name, (a, b) in vals.items()}
        unexplained = change - (p1 - p0)
        # 所有贡献按同一绝对精度（最大量级的 15 位有效数字）舍入；剩下的几个末位单位只是表示尾差，
        # 并入绝对值最大的贡献，使贡献之和精确等于因子之积的变化
        scale = max([abs(p1 - p0), *map(abs, exact.values())])
        step = Decimal(1).scaleb(scale.adjusted() - 14) if scale else Decimal(1)
        contributions = {n: c.quantize(step) for n, c in exact.items()}
        if contributions:
            largest = max(contributions, key=lambda n: abs(contributions[n]))
            contributions[largest] += (p1 - p0).quantize(step) - sum(contributions.values(), Decimal(0))
        contributions = {n: c if c else Decimal(0) for n, c in contributions.items()}  # 零写成 0，不写 0E-17
    rows = [{"factor": n, "previous": vals[n][0], "current": vals[n][1],
             "change_rate": _q((vals[n][1] - vals[n][0]) / vals[n][0]), "contribution": contributions[n]} for n in exact]
    if unexplained:
        rows.append({"factor": "未解释差额", "previous": None, "current": None, "change_rate": None,
                     "contribution": unexplained})
    for r in rows:
        r["share"] = _q(r["contribution"] / change) if change else None
    return {"total": {"previous": t0, "current": t1, "change": change}, "factors": rows,
            "unexplained": unexplained}


# --- 日历折算 -----------------------------------------------------------------------

def period_days(start: date, end: date, holidays: set[date] | None = None) -> dict[str, int | None]:
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    return {"days": len(days), "weekend_days": sum(1 for d in days if d.weekday() >= 5),
            "holidays": sum(1 for d in days if d in holidays) if holidays is not None else None}


def calendar_adjust(current: Any, previous: Any, current_period: tuple[date, date],
                    previous_period: tuple[date, date], holidays: set[date] | None = None) -> dict[str, Any]:
    """只按自然日天数折算；周六日天数供参考，未配置业务日历时节假日未知。"""
    cur, prev = number(current), number(previous)
    if cur is None or prev is None:
        raise CalcError("两期值都需要")
    dc, dp = period_days(*current_period, holidays), period_days(*previous_period, holidays)
    daily_cur, daily_prev = cur / dc["days"], prev / dp["days"]
    # 本期天数按上期日均值折算出的“天数效应”；其余为日均水平的变化
    day_effect = daily_prev * (dc["days"] - dp["days"])
    return {"current": {**dc, "value": cur, "daily": _significant12(daily_cur)},
            "previous": {**dp, "value": prev, "daily": _significant12(daily_prev)},
            "change": cur - prev, "day_count_effect": _significant12(day_effect),
            "daily_level_effect": _significant12(cur - prev - day_effect),
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

def period_labels(start: date, end: date, grain: str) -> list[str]:
    """与问数的期间列一致：周用周一，季用季初日期，月与年保留相应精度。"""
    if grain not in {"day", "week", "month", "quarter", "year"}:
        raise CalcError("时间粒度未知，无法判断期间覆盖度")
    if start > end:
        raise CalcError("覆盖度的开始日期不能晚于结束日期")
    if grain == "week":
        cursor = start - timedelta(days=start.weekday())
    elif grain == "quarter":
        cursor = date(start.year, (start.month - 1) // 3 * 3 + 1, 1)
    elif grain == "year":
        cursor = date(start.year, 1, 1)
    elif grain == "month":
        cursor = start.replace(day=1)
    else:
        cursor = start
    labels = []
    while cursor <= end:
        labels.append(cursor.strftime("%Y-%m") if grain == "month" else (
            str(cursor.year) if grain == "year" else cursor.isoformat()))
        if grain in {"day", "week"}:
            step = 1 if grain == "day" else 7
            if (end - cursor).days < step:
                break
            cursor += timedelta(days=step)
        elif grain == "year":
            if cursor.year == 9999:
                break
            cursor = date(cursor.year + 1, 1, 1)
        else:
            month = cursor.month + (1 if grain == "month" else 3)
            year = cursor.year + (month - 1) // 12
            if year > 9999:
                break
            cursor = date(year, (month - 1) % 12 + 1, 1)
    return labels


def coverage(rows: list[Row], keys: list[str], period: str | None, start: date | None = None,
             end: date | None = None, *, grain: str | None = None) -> dict[str, Any]:
    """各成员的行数与期间覆盖度；只有粒度明确，或原有日日期证据完整时判断缺口。"""
    expected = None
    daily = bool(period and rows)
    if daily:
        for row in rows:
            value = row.get(period)
            try:
                daily = isinstance(value, str) and date.fromisoformat(value).isoformat() == value
            except ValueError:
                daily = False
            if not daily:
                break
    if period and grain and start and end:
        expected = period_labels(start, end, grain)
        for row in rows:
            value = row.get(period)
            try:
                parsed = date.fromisoformat(f"{value}-01" if grain == "month" else (
                    f"{value}-01-01" if grain == "year" else value))
                valid = period_labels(parsed, parsed, grain) == [value]
            except (TypeError, ValueError):
                valid = False
            if not valid:
                raise CalcError("期间列与已声明的时间粒度不一致，无法判断覆盖度")
    elif daily and start and end:
        expected = period_labels(start, end, "day")
    groups: dict[tuple, list[Row]] = {}
    for r in rows:
        groups.setdefault(tuple(r.get(k) for k in keys), []).append(r)
    if not groups and grain and not keys:
        groups[()] = []
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
