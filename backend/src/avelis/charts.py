"""只依赖列契约的确定性选图；规格包含已排序、截断和透视的完整展示数据。"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass

from avelis.analysis.grounding import number
from avelis.i18n import gettext


@dataclass(frozen=True)
class _Structured:
    """数组、JSON 等结构化单元格的分组键：带类型比较，不会与内容相同的普通字符串合并；显示时转成文本。"""

    text: str

    def __str__(self) -> str:
        return self.text


def _number(value) -> float | None:
    parsed = number(value)
    if parsed is None:
        return None
    result = float(parsed)
    return result if math.isfinite(result) else None


def _sum(values) -> float | None:
    present = [value for value in values if value is not None]
    return _number(sum(present)) if present else None


def _comparison_groups(columns: list[str], roles: dict) -> list[tuple[str, str, str, str]]:
    groups = []
    index = 0
    while index + 3 < len(columns):
        current, previous, change, rate = columns[index : index + 4]
        # 本期可以是 SQL 别名；只按连续四列契约定位，不从别名猜业务指标。
        base, separator, suffix = change.rpartition("·")
        if (
            [roles.get(column, {}).get("role") for column in (current, previous, change, rate)]
            == ["measure", "measure", "change", "rate"]
            and separator
            and suffix == "变化"
            and rate == f"{base}·变化率"
            and previous.startswith(f"{base}·")
            and previous != f"{base}·"
        ):
            groups.append((current, previous, change, rate))
            index += 4
        else:
            index += 1
    return groups


def recommend(
    columns: list[str],
    rows: list[list],
    roles: dict,
    units: dict,
    polarity: dict,
    intent: dict | None = None,
    preference: dict | None = None,
) -> list[dict]:
    """校验可选展示偏好；非法偏好整体忽略，原始查询结果不变。"""
    specs = _recommend(columns, rows, roles, units, polarity, intent)
    if not isinstance(preference, dict) or set(preference) - {"kind", "title", "top_n"}:
        return specs
    kind, title, top_n = (preference.get(key) for key in ("kind", "title", "top_n"))
    selected = next((s for s in specs if s["kind"] == kind and kind != "kpi"), None)
    if (
        selected is None
        or (title is not None and (not isinstance(title, str) or len(title) > 40))
        or (top_n is not None and (type(top_n) is not int or not 2 <= top_n <= 20))
    ):
        return specs
    if top_n is not None and (
        (selected["orientation"] == "horizontal" and kind in ("bar", "grouped_bar", "diverging_bar"))
        or (kind == "multiples" and selected["x"] and selected["x"]["role"] == "dimension")
    ):
        tuned = _recommend(columns, rows, roles, units, polarity, intent, top_n=top_n)
        selected = next(s for s in tuned if s["kind"] == kind)
    selected = {**selected, "title": title}
    return [selected, *(s for s in specs if s["kind"] != kind)]


def _recommend(
    columns: list[str],
    rows: list[list],
    roles: dict,
    units: dict,
    polarity: dict,
    intent: dict | None = None,
    *,
    top_n: int | None = None,
) -> list[dict]:
    """返回默认图及同数据候选的已物化规格；没有合适图表时返回空列表。"""
    intent = intent or {}
    limit = top_n or 20
    by_role = {
        role: [c for c in columns if roles.get(c, {}).get("role") == role]
        for role in ("time", "dimension", "measure", "change")
    }
    if not rows or not (by_role["measure"] or by_role["change"]):
        return []
    if len(set(columns)) != len(columns):
        return []  # 列名重复（如两个业务域的同名口径）时按列名取值会张冠李戴，不出图，界面保留表格。
    indices = {column: index for index, column in enumerate(columns)}

    def cell(row, column):
        index = indices[column]
        raw = row[index] if index < len(row) else None
        # 探索结果可能保留数组、JSON 等原生值：转成稳定的文本作键，并保留“结构化”这一类型。
        if isinstance(raw, (list, dict, tuple, set)):
            return _Structured(
                json.dumps(
                    sorted(raw) if isinstance(raw, set) else raw, ensure_ascii=False, sort_keys=True, default=str
                )
            )
        return raw

    def value(row, column):
        return _number(cell(row, column))

    def additive(column):
        # 只认登记过的可加性（受治理口径按表达式确认）；探索、计算结果与旧结果没有登记时不做相加类变换。
        return (units.get(column) or {}).get("additive") is True

    def series(column, values, *, name=None, mark="bar", axis=0, direction=None, other=False):
        label = roles[column].get("label") or column
        return {
            "name": label if name is None else name,
            "column": column,
            "label": label,
            "axis": axis,
            "mark": mark,
            "other": other,
            "unit": dict(units.get(column) or {"kind": "unknown"}),
            "polarity": polarity.get(direction or column),
            "values": values,
        }

    def spec(kind, reason, x, categories, series_list, *, orientation="vertical", others=None, dropped=0, kpis=None):
        return {
            "kind": kind,
            "title": None,
            "orientation": orientation,
            "reason": reason,
            "x": {
                "column": x,
                "label": roles[x].get("label") or x,
                "role": roles[x]["role"],
                "grain": roles[x].get("grain", intent.get("time_grain")) if roles[x]["role"] == "time" else None,
            }
            if x
            else None,
            "categories": [str(c) if isinstance(c, _Structured) else c for c in categories],
            "series": series_list,
            "kpis": kpis or [],
            "others": others,
            "dropped_series": dropped,
        }

    def alternate(chart, kind, mark):
        return {**chart, "kind": kind, "series": [{**s, "mark": mark} for s in chart["series"]]}

    def ordered(items, key, *, asc=False):
        # 空值始终排最后；同值沿用输入顺序，不依赖集合遍历顺序。
        return sorted(items, key=lambda item: (key(item) is None, (key(item) or 0) * (1 if asc else -1)))

    groups = _comparison_groups(columns, roles)
    previous_columns = {group[1] for group in groups}
    measures = [column for column in by_role["measure"] if column not in previous_columns] or by_role["change"]
    time = next(iter(by_role["time"]), None)
    dimensions = by_role["dimension"]
    dimension = next(iter(dimensions), None)

    def member_key(row):
        # 两个及以上维度时用全部维度值的元组作分组键（不会因值里含“ / ”而撞车），显示时再拼成“甲 / 乙”。
        if len(dimensions) >= 2:
            return tuple(cell(row, column) for column in dimensions)
        return cell(row, dimension)

    def label(key):
        return " / ".join(str(part) for part in key) if isinstance(key, tuple) else key

    category = member_key

    if not time and not dimension and len(rows) == 1:
        comparisons = {group[0]: group for group in groups}
        kpis = []
        for column in measures:
            group = comparisons.get(column)
            kpis.append(
                {
                    "label": roles[column].get("label") or column,
                    "column": column,
                    "unit": dict(units.get(column) or {"kind": "unknown"}),
                    "polarity": polarity.get(column),
                    "value": value(rows[0], column),
                    "previous": value(rows[0], group[1]) if group else None,
                    "change": value(rows[0], group[2]) if group else None,
                    "rate": value(rows[0], group[3]) if group else None,
                    "previous_label": gettext(group[1].rsplit("·", 1)[1]) if group else None,
                }
            )
        return [spec("kpi", "kpi", None, [], [], kpis=kpis)]

    if groups and dimension and len(rows) >= 2:
        current, previous, change, _ = groups[0]

        def comparison_data(sort_column, columns):
            ranked = ordered(rows, lambda row: value(row, sort_column))
            kept, tail = ranked[:limit], ranked[limit:]
            categories = [label(category(row)) for row in kept]
            values = [[value(row, column) for row in kept] for column in columns]
            merged = None
            if top_n is not None and tail and all(additive(c) for c in columns):
                merged = {"count": len(tail)}
                categories.append(gettext("其他"))
                for column, items in zip(columns, values, strict=True):
                    items.append(_sum(value(row, column) for row in tail))
            return categories, values, merged

        categories, values, merged = comparison_data(change, [change])
        default = spec(
            "diverging_bar",
            "change",
            dimension,
            categories,
            [series(change, values[0], direction=current)],
            orientation="horizontal",
            others=merged,
        )
        categories, values, merged = comparison_data(current, [current, previous])
        candidate = spec(
            "grouped_bar",
            "comparison",
            dimension,
            categories,
            [
                series(current, values[0], name=gettext("本期")),
                series(previous, values[1], name=gettext(previous.rsplit("·", 1)[1])),
            ],
            orientation="horizontal",
            others=merged,
        )
        return [default, candidate]

    def pivot(x_column, split, column):
        categories = list(dict.fromkeys(cell(row, x_column) for row in rows))
        keys = list(dict.fromkeys(split(row) for row in rows))
        cells = defaultdict(list)
        for row in rows:
            cells[cell(row, x_column), split(row)].append(value(row, column))
        values = {key: _sum(items) if additive(column) else items[0] for key, items in cells.items()}
        return categories, keys, values

    def multi(x, categories, values, *, temporal=False):
        same_unit = (
            len(
                {
                    (
                        (units.get(c) or {}).get("kind", "unknown"),
                        (units.get(c) or {}).get("unit"),
                        str((units.get(c) or {}).get("scale") or 1),
                    )
                    for c in measures
                }
            )
            == 1
        )
        reason = "multi_measure" if len(measures) > 1 else "time_series"
        if not temporal:
            reason = "multi_measure"
        if not same_unit:
            chart = spec(
                "multiples",
                reason,
                x,
                categories,
                [
                    series(column, items, mark="line" if temporal else "bar", axis=index)
                    for index, (column, items) in enumerate(zip(measures[:4], values[:4], strict=True))
                ],
                dropped=max(0, len(measures) - 4),
            )
            mark = "line" if temporal else "bar"
            candidate = spec(
                "line" if temporal else "bar",
                reason,
                x,
                categories,
                [series(measures[0], values[0], mark=mark)],
                orientation="vertical" if temporal else "horizontal",
            )
            return [chart, candidate]
        chart = spec(
            "line" if temporal else "grouped_bar",
            reason,
            x,
            categories,
            [series(c, v, mark="line" if temporal else "bar") for c, v in zip(measures, values, strict=True)],
            orientation="vertical" if temporal else "horizontal",
        )
        if temporal:
            return [chart, alternate(chart, "bar" if len(measures) == 1 else "grouped_bar", "bar")]
        return [chart]

    if time:
        periods = sorted(dict.fromkeys(cell(row, time) for row in rows), key=str)
        if len(periods) >= 2:
            if dimension:
                column = measures[0]
                _, members, values = pivot(time, member_key, column)
                merged, dropped = None, 0
                selected = members
                if len(members) > 8:
                    ranked = ordered(
                        members, lambda member: abs(sum(values.get((period, member)) or 0 for period in periods))
                    )
                    selected = ranked[:7]
                    if additive(column):
                        merged = {"count": len(ranked) - 7}
                    else:
                        dropped = len(ranked) - 7
                lines = [
                    series(
                        column,
                        [values.get((period, member)) for period in periods],
                        name=str(label(member)),
                        mark="line",
                    )
                    for member in selected
                ]
                if merged:
                    lines.append(
                        series(
                            column,
                            [_sum(values.get((period, member)) for member in ranked[7:]) for period in periods],
                            name=gettext("其他"),
                            mark="line",
                            other=True,
                        )
                    )
                chart = spec("line", "time_by_dimension", time, periods, lines, others=merged, dropped=dropped)
                return [chart, alternate(chart, "stacked_bar" if additive(column) else "grouped_bar", "bar")]
            # 时间分组结果通常每期一行；同一格的可加值合计，不可加值保留首行。
            values = []
            for column in measures:
                cells = defaultdict(list)
                for row in rows:
                    cells[cell(row, time)].append(value(row, column))
                values.append([_sum(cells[p]) if additive(column) else cells[p][0] for p in periods])
            return multi(time, periods, values, temporal=True)
        if not dimension:
            return []

    if not dimension:
        return []
    grouped = defaultdict(list)
    for row in rows:
        grouped[category(row)].append(row)
    values = {
        category: [
            _sum(value(row, column) for row in items) if additive(column) else value(items[0], column)
            for column in measures
        ]
        for category, items in grouped.items()
    }
    categories = ordered(list(grouped), lambda category: values[category][0], asc=intent.get("order") == "asc")
    tail = categories[limit:]
    categories = categories[:limit]
    materialized = [[values[category][index] for category in categories] for index in range(len(measures))]
    categories = [label(category) for category in categories]
    if len(measures) >= 2:
        result = multi(dimension, categories, materialized)
        for chart in result:
            if top_n is not None and tail and all(additive(s["column"]) for s in chart["series"]):
                chart["others"] = {"count": len(tail)}
                chart["categories"] = [*categories, gettext("其他")]
                for s in chart["series"]:
                    index = measures.index(s["column"])
                    s["values"] = [*s["values"], _sum(values[category][index] for category in tail)]
        return result
    column = measures[0]
    merged = None
    if tail and additive(column):
        merged = {"count": len(tail)}
        categories.append(gettext("其他"))
        materialized[0].append(_sum(values[category][0] for category in tail))
    reason = "ranking" if intent.get("order") is not None or intent.get("limit") is not None else "composition"
    chart = spec(
        "bar", reason, dimension, categories, [series(column, materialized[0])], orientation="horizontal", others=merged
    )
    if additive(column) and len(categories) <= 8 and all(v is not None and v >= 0 for v in materialized[0]):
        return [chart, alternate(chart, "donut", "bar")]
    return [chart]
