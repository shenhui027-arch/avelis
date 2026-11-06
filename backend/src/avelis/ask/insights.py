"""从回答的展示契约提取要点；只计算已有结果，不解释原因或推测业务含义。"""

from __future__ import annotations

import math
from collections import defaultdict
from decimal import Decimal, DecimalException, localcontext

from avelis.analysis.grounding import _unit, display, number
from avelis.analysis.measures import like
from avelis.charts import _category_order, _comparison_groups, _number, additive, category_label, selected_measures
from avelis.i18n import gettext


def build(
    columns: list[str],
    rows: list[list],
    column_roles: dict,
    units: dict,
    polarity: dict,
    *,
    time_aggregations: dict | None = None,
    truncated: bool = False,
) -> list[dict]:
    """按 total、change、peak、low、share、spread 排序，最多给四条完整的显示文案。"""
    if truncated or len(rows) <= 1 or len(set(columns)) != len(columns):
        return []
    measures = selected_measures(columns, column_roles)
    if not measures:
        return []
    column = measures[0]
    indices = {name: index for index, name in enumerate(columns)}

    def cell(row, name):
        index = indices[name]
        return row[index] if index < len(row) else None

    values = [(row, number(cell(row, column))) for row in rows]
    present = [(row, value) for row, value in values if value is not None]
    if not present:
        return []
    meta = units.get(column) or {}
    # 数值存在但展示不了时不能删掉它再声称剩余值是合计或最高项。
    try:
        if any(_number(value) is None for _, value in present):
            return []
        for _, value in present:
            display(value, _unit(meta))
    except DecimalException:
        return []
    can_add = (
        column_roles.get(column, {}).get("role") == "measure"
        and additive(column, units)
        and meta.get("kind") not in {"ratio", "rate_change", "share"}
    )
    name = column_roles.get(column, {}).get("label") or column
    dimensions = [c for c in columns if column_roles.get(c, {}).get("role") == "dimension"]
    time = next((c for c in columns if column_roles.get(c, {}).get("role") == "time"), None)
    time_aggregation = (time_aggregations or {}).get(column)
    # 维度可加不代表时间可加；旧答案缺时间语义时，带时间列的合计保守隐藏。
    can_total = can_add and (
        not time
        or time_aggregation == "sum"
        or (
            time_aggregation is not None
            and cell(rows[0], time) is not None
            and all(cell(row, time) == cell(rows[0], time) for row in rows)
        )
    )
    categories = [*([time] if time else []), *dimensions]
    result = []

    def formatted(value, unit=None, *, direction=None):
        try:
            return display(value, _unit(meta if unit is None else unit), direction=direction)
        except DecimalException:
            return None

    def sentence(template, **params):
        return gettext(template, **params) if all(value is not None for value in params.values()) else None

    def label(row, names=categories):
        return " / ".join(category_label(cell(row, c)) for c in names)

    def add(kind, text, value, category=None):
        if text is None:
            return
        numeric = float(value)
        result.append(
            {
                "kind": kind,
                "text": text,
                "column": column,
                "category": category,
                "value": numeric if math.isfinite(numeric) else None,
            }
        )

    with localcontext(prec=80):  # 覆盖 DECIMAL(38) 聚合及进位，不改变共享的显示精度。
        total = sum((value for _, value in present), Decimal(0))
    if can_total:
        add("total", sentence("{measure}合计为{value}。", measure=name, value=formatted(total)), total)

    comparison = next((group for group in _comparison_groups(columns, column_roles) if group[0] == column), None)
    if comparison and can_total:
        paired = [(number(cell(row, comparison[0])), _comparison_number(cell(row, comparison[1]))) for row in rows]
        # 缺一侧的成员不当作零，否则总变化会包含无法确认的差额。
        if all(current is not None and previous is not None for current, previous in paired):
            with localcontext(prec=80):
                current = sum((current for current, _ in paired), Decimal(0))
                previous = sum((previous for _, previous in paired), Decimal(0))
            _change(
                add, name, current, previous, formatted, meta, gettext("本期"), gettext(comparison[1].rsplit("·", 1)[1])
            )
    elif comparison:
        comparable = [
            (row, number(cell(row, comparison[2])))
            for row in rows
            if all(_comparison_number(cell(row, c)) is not None for c in comparison[:3])
        ]
        if comparable:
            row, delta = max(comparable, key=lambda item: abs(item[1]))
            _change(
                add,
                gettext("{category}的{measure}", category=label(row), measure=name) if categories else name,
                number(cell(row, comparison[0])),
                number(cell(row, comparison[1])),
                formatted,
                meta,
                gettext("本期"),
                gettext(comparison[1].rsplit("·", 1)[1]),
                delta=delta,
                rate=number(cell(row, comparison[3])),
                category=label(row) if categories else None,
            )
    elif time:
        periods = defaultdict(list)
        for row, value in values:
            if cell(row, time) is not None:
                periods[category_label(cell(row, time))].append(value)
        ordered = sorted(periods, key=_category_order)
        if len(ordered) >= 2 and (can_add or all(len(items) == 1 for items in periods.values())):
            first, last = ordered[0], ordered[-1]
            if all(value is not None for key in (first, last) for value in periods[key]):
                with localcontext(prec=80):
                    previous = sum(periods[first], Decimal(0))
                    current = sum(periods[last], Decimal(0))
                _change(add, name, current, previous, formatted, meta, last, first)

    if categories:
        for kind, choose in (("peak", max), ("low", min)):
            if kind == "low" and len(rows) < 3:
                continue
            row, value = choose(present, key=lambda item: item[1])
            category = label(row)
            template = (
                "{category}的{measure}最高，为{value}。" if kind == "peak" else "{category}的{measure}最低，为{value}。"
            )
            add(kind, sentence(template, category=category, measure=name, value=formatted(value)), value, category)

    if dimensions and can_total and all(value is not None and value >= 0 for _, value in values) and total > 0:
        members = defaultdict(Decimal)
        with localcontext(prec=80):
            for row, value in present:
                members[tuple(cell(row, c) for c in dimensions)] += value
        ranked = sorted(members.items(), key=lambda item: item[1], reverse=True)
        if len(ranked) >= 2:
            leader, largest = ranked[0]
            leader = " / ".join(category_label(value) for value in leader)
            share = largest / total
            text = sentence(
                "{category}的{measure}占合计的{share}。",
                category=leader,
                measure=name,
                share=formatted(share, {"kind": "share"}),
            )
            if share < Decimal("0.5"):
                combined = Decimal(0)
                for count, (_, value) in enumerate(ranked, start=1):
                    with localcontext(prec=80):
                        combined += value
                    if combined / total >= Decimal("0.5"):
                        text = sentence(
                            "{category}的{measure}占合计的{share}；前 {count} 项合计占{combined_share}。",
                            category=leader,
                            measure=name,
                            share=formatted(share, {"kind": "share"}),
                            count=count,
                            combined_share=formatted(combined / total, {"kind": "share"}),
                        )
                        break
            add("share", text, share, leader)

    if dimensions and time and len(result) < 4:
        periods = defaultdict(dict)
        for row, value in present:
            period, member = category_label(cell(row, time)), tuple(cell(row, c) for c in dimensions)
            if member in periods[period]:
                # 同一时间与成员仍有多行时，只有可加度量才可合并。
                old = periods[period][member]
                with localcontext(prec=80):
                    periods[period][member] = old + value if can_add and old is not None else None
            else:
                periods[period][member] = value
        gaps = []
        for period, members in periods.items():
            comparable = [(member, value) for member, value in members.items() if value is not None]
            if len(comparable) >= 2:
                high = max(comparable, key=lambda item: item[1])
                low = min(comparable, key=lambda item: item[1])
                with localcontext(prec=80):
                    gaps.append((high[1] - low[1], period, high[0], low[0]))
        if gaps:
            gap, period, high, low = max(gaps, key=lambda item: item[0])
            add(
                "spread",
                sentence(
                    "{period}的成员间差距最大，{high}与{low}的{measure}相差{value}。",
                    period=period,
                    high=" / ".join(category_label(value) for value in high),
                    low=" / ".join(category_label(value) for value in low),
                    measure=name,
                    value=formatted(gap, like(meta, "delta"), direction="up" if gap > 0 else None),
                ),
                gap,
                period,
            )
    return result[:4]


def _change(
    add,
    name,
    current,
    previous,
    formatted,
    meta,
    current_label,
    previous_label,
    *,
    delta=None,
    rate=None,
    category=None,
):
    with localcontext(prec=80):
        delta = current - previous if delta is None else delta
    values = {
        "measure": name,
        "previous_label": previous_label,
        "current_label": current_label,
        "previous": formatted(previous),
        "current": formatted(current),
        "change": formatted(delta, like(meta, "delta")),
    }
    if any(value is None for value in values.values()):
        return
    if previous != 0:
        try:
            shown_rate = formatted(delta / previous if rate is None else rate, {"kind": "rate_change"})
        except DecimalException:
            return
        if shown_rate is None:
            return
        text = gettext(
            "{measure}从{previous_label}的{previous}变为{current_label}的{current}，变化{change}，变化率为{rate}。",
            **values,
            rate=shown_rate,
        )
    else:
        text = gettext(
            "{measure}从{previous_label}的{previous}变为{current_label}的{current}，变化{change}；基期为零，无法计算变化率。",
            **values,
        )
    add("change", text, delta, category or current_label)


def _comparison_number(value):
    """比较期也沿用图表的有限数值边界，避免汇总时触发 Decimal 指数溢出。"""
    return number(value) if _number(value) is not None else None
