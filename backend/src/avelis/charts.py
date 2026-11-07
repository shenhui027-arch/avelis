"""只依赖列契约的确定性选图；规格包含已排序、截断和透视的完整展示数据。"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from itertools import zip_longest
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from avelis.analysis.grounding import number
from avelis.i18n import gettext

MAX_PANELS = 8


def category_label(value) -> str:
    """类目的显示名：与页面（chartOption 的类目轴，JS 的 String()）和导出一致；highlight 按它匹配。

    空值为“—”；布尔值写成 true / false；整数值的浮点数去掉“.0”（String(1.0) 是 "1"）。"""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class ChartPreference(BaseModel):
    """图表偏好的唯一契约；两个智能体与保存偏好的投影均从这里派生。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal[
        "line",
        "bar",
        "grouped_bar",
        "stacked_bar",
        "diverging_bar",
        "donut",
        "multiples",
        "area",
        "combo",
        "pareto",
        "waterfall",
        "heatmap",
        "scatter",
    ] = Field(
        description=(
            "只能选该数据集可画的图种；area 展示少量时间系列，combo 展示不同单位的两个度量，"
            "pareto 展示非负可加度量的累计集中度，waterfall 展示成员对期间变化的贡献，"
            "heatmap 展示时间与成员交叉的度量，scatter 展示两个度量的关系及可选的第三个大小度量"
        )
    )
    title: str = Field(max_length=40, description="用一句结论概括结果，不超过 40 字")
    top_n: int | None = Field(None, ge=2, le=20, description="可选：横向条形或排名图保留前 N 项，其他图忽略")
    measures: list[str] | None = Field(
        None,
        min_length=1,
        exclude_if=lambda value: value is None,
        description="可选：按顺序指定度量列名或对比指标名；不传时画全部",
    )
    sort: Literal["value_desc", "value_asc", "category"] | None = Field(
        None,
        exclude_if=lambda value: value is None,
        description="可选：类目图按第一条所选系列的值降序、升序或类目自然顺序排列；时间序列不接受此参数",
    )
    marks: list[Literal["max", "min", "average"]] | None = Field(
        None,
        exclude_if=lambda value: value is None,
        description="可选：每条系列标出最高点、最低点或平均线；average 是所示非空值的算术平均（比率也不加权）",
    )
    highlight: list[str] | None = Field(
        None,
        max_length=5,
        exclude_if=lambda value: value is None,
        description="可选：最多 5 个所示类目名，按完整显示值精确匹配；其余类目弱化",
    )

    @field_validator("marks", "highlight")
    @classmethod
    def distinct_items(cls, value):
        return list(dict.fromkeys(value)) if value is not None else None

    @classmethod
    def from_record(cls, record: dict) -> dict:
        return {name: record[name] for name in cls.model_fields if name in record}

    @classmethod
    def tool_description(cls) -> str:
        return "；".join(f"{name}：{field.description}" for name, field in cls.model_fields.items()) + "。"

    def options(self, columns, rows, roles, units, polarity, intent=None) -> list[dict]:
        """严格校验展示偏好；工具调用失败时保留可选项，读取旧偏好时由 recommend 回退。"""
        selected_measures(columns, roles, self.measures)
        options = _recommend(columns, rows, roles, units, polarity, intent, measures=self.measures)
        selected = next((option for option in options if option["kind"] == self.kind), None)
        if selected is None:
            return options
        if self.marks is not None and self.kind in ("heatmap", "scatter", "waterfall"):
            raise ValueError(gettext("{kind} 不支持 marks；请省略 marks。", kind=self.kind))
        if self.kind == "pareto" and self.sort not in (None, "value_desc"):
            raise ValueError(gettext("pareto 只支持 value_desc 排序；请省略 sort 或使用 value_desc。"))
        if self.kind == "waterfall" and self.sort is not None:
            raise ValueError(gettext("waterfall 按变化绝对值降序排列；请省略 sort。"))
        if self.sort is not None and (selected.get("x") or {}).get("role") != "dimension":
            raise ValueError(gettext("时间序列图不支持 sort；请省略 sort，时间轴保持自然顺序。"))
        if self.top_n is not None and (
            (selected["orientation"] == "horizontal" and self.kind in ("bar", "grouped_bar", "diverging_bar"))
            or (self.kind == "multiples" and (selected.get("x") or {}).get("role") == "dimension")
            or self.kind == "pareto"
        ):
            limit = self.top_n
        else:
            limit = None
        if limit is not None or self.sort is not None:
            tuned = _recommend(
                columns,
                rows,
                roles,
                units,
                polarity,
                intent,
                top_n=limit,
                measures=self.measures,
                sort=self.sort,
            )
            selected = next((option for option in tuned if option["kind"] == self.kind), None)
            if selected is None:
                return tuned
        available = [category_label(category) for category in selected["categories"]]
        missing = [category for category in self.highlight or [] if category not in available]
        if missing:
            raise ValueError(
                gettext(
                    "类目 {categories} 不在所示图中；可选 highlight：{available}",
                    categories="、".join(missing),
                    available="、".join(available) or gettext("无"),
                )
            )
        marks = _materialized_marks(selected, self.marks) if self.marks is not None else None
        if self.marks and not marks:
            raise ValueError(gettext("所示系列没有可标注的数值；请取消 marks 或选择其他 measures。"))
        selected = {
            **selected,
            "title": self.title,
            "marks": marks,
            "highlight": list(self.highlight) if self.highlight is not None else None,
        }
        return [selected, *(option for option in options if option["kind"] != self.kind)]

    def materialize(self, columns, rows, roles, units, polarity, intent=None) -> dict:
        """返回已严格校验的所选图；不支持的图种报错并列出可选项。"""
        options = self.options(columns, rows, roles, units, polarity, intent)
        selected = next((option for option in options if option["kind"] == self.kind), None)
        if selected is None:
            raise ValueError(
                gettext(
                    "不支持 {kind}；可选 kind：{available}",
                    kind=self.kind,
                    available="、".join(option["kind"] for option in options if option["kind"] != "kpi")
                    or gettext("无"),
                )
            )
        return selected


def _materialized_marks(chart: dict, requested: list[str]) -> list[dict]:
    marks = []
    for series_index, series in enumerate(chart["series"]):
        present = [(index, value) for index, value in enumerate(series["values"]) if value is not None]
        if not present:
            continue
        for kind in requested:
            if kind == "average":
                # 这是已显示值的均值，不把比率说成总体比率，也不将合并的“其他”拆回原成员。
                value = _number(sum(value / len(present) for _, value in present))
                x = None
            else:
                index, value = (max if kind == "max" else min)(present, key=lambda item: item[1])
                x = chart["categories"][index]
            if value is not None:
                marks.append(
                    {"type": kind, "series": series["column"], "series_index": series_index, "x": x, "value": value}
                )
    return marks


def _category_order(value) -> tuple:
    """按显示值自然排序；数字片段按数值比较，空类目置后。"""
    if value is None:
        return (True, ())
    numeric = _number(value)
    if numeric is not None:
        return (False, ((0, numeric),))
    return (
        False,
        tuple((0, int(part)) if part.isdigit() else (1, part.casefold()) for part in re.split(r"(\d+)", str(value))),
    )


def _unit_identity(unit: dict | None) -> tuple:
    unit = unit or {}
    return (unit.get("kind", "unknown"), unit.get("unit"), str(unit.get("scale") or 1), bool(unit.get("of_ratio")))


def additive(column: str, units: dict) -> bool:
    """只认登记过的可加性；未登记的探索、计算和旧结果不做相加类变换。"""
    return (units.get(column) or {}).get("additive") is True


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


def selected_measures(columns: list[str], roles: dict, requested: list[str] | None = None) -> list[str]:
    groups = _comparison_groups(columns, roles)
    previous = {group[1] for group in groups}
    available = [c for c in columns if roles.get(c, {}).get("role") == "measure" and c not in previous]
    available = available or [c for c in columns if roles.get(c, {}).get("role") == "change"]
    aliases: dict[str, str | None] = {column: column for column in available}
    # 模型看到的是显示名（问数解释用 column_roles 的 label）：唯一的显示名也能指定度量；
    # 重复的显示名有歧义，要求改用列名。
    labels: dict[str, list[str]] = {}
    for column in available:
        label = roles.get(column, {}).get("label")
        if label and label != column:
            labels.setdefault(label, []).append(column)
    for label, owners in labels.items():
        aliases.setdefault(label, owners[0] if len(owners) == 1 else None)
    aliases.update({group[2].rpartition("·")[0]: group[0] for group in groups})
    if requested is None:
        return available
    ambiguous = [name for name in requested if name in aliases and aliases[name] is None]
    if ambiguous:
        raise ValueError(
            gettext(
                "度量名 {measures} 对应多列，请改用列名：{columns}",
                measures="、".join(ambiguous),
                columns="、".join(c for name in ambiguous for c in labels[name]),
            )
        )
    missing = [name for name in requested if name not in aliases]
    if missing:
        raise ValueError(
            gettext(
                "度量 {measures} 不存在；可选 measures：{available}",
                measures="、".join(missing),
                available="、".join(name for name, column in aliases.items() if column is not None) or gettext("无"),
            )
        )
    return list(dict.fromkeys(aliases[name] for name in requested))


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
    if not isinstance(preference, dict):
        return specs
    try:
        choice = ChartPreference.model_validate(
            {**preference, "title": preference["title"] if preference.get("title") is not None else ""}
        )
        selected_specs = choice.options(columns, rows, roles, units, polarity, intent)
    except (ValidationError, ValueError):
        return specs
    selected = next((s for s in selected_specs if s["kind"] == choice.kind), None)
    if selected is None:
        return selected_specs if choice.kind == "diverging_bar" else specs
    selected = {**selected, "title": preference.get("title")}
    return [selected, *(s for s in selected_specs if s["kind"] != choice.kind)]


def _recommend(
    columns: list[str],
    rows: list[list],
    roles: dict,
    units: dict,
    polarity: dict,
    intent: dict | None = None,
    *,
    top_n: int | None = None,
    measures: list[str] | None = None,
    sort: str | None = None,
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

    def omitted(count, merged=None):
        # 合并进“其他”的成员仍被展示，不再计入省略；规格的类目与系列只包含展示数据。
        return (
            {"count": count, "reason": "top_n" if top_n is not None else "display_limit"}
            if count and not merged
            else None
        )

    def spec(
        kind,
        reason,
        x,
        categories,
        series_list,
        *,
        orientation="vertical",
        others=None,
        dropped=0,
        kpis=None,
        omitted_categories=None,
    ):
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
            "omitted_categories": omitted_categories,
            "dropped_series": dropped,
            "marks": None,
            "highlight": None,
        }

    def alternate(chart, kind, mark):
        return {**chart, "kind": kind, "series": [{**s, "mark": mark} for s in chart["series"]]}

    def ordered(items, key, *, asc=False):
        # 空值始终排最后；同值沿用输入顺序，不依赖集合遍历顺序。
        return sorted(items, key=lambda item: (key(item) is None, (key(item) or 0) * (1 if asc else -1)))

    groups = _comparison_groups(columns, roles)
    measures = selected_measures(columns, roles, measures)
    comparisons = {group[0]: group for group in groups}
    groups = [comparisons[column] for column in measures if column in comparisons]
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

    def area_candidate(charts):
        line = next((chart for chart in charts if chart["kind"] == "line"), None)
        if line is not None and 1 <= len(line["series"]) <= 3:
            return [alternate(line, "area", "line")]
        return []

    def combo_candidate(x, categories, values):
        if len(measures) != 2 or (
            len({_unit_identity(units.get(column)) for column in measures}) == 1
            and not any((units.get(column) or {}).get("kind") == "ratio" for column in measures)
        ):
            return []
        return [
            spec(
                "combo",
                "multi_measure",
                x,
                categories,
                [series(measures[0], values[0], mark="bar"), series(measures[1], values[1], mark="line", axis=1)],
            )
        ]

    def scatter_candidate():
        if len(measures) < 2 or len(rows) < 3:
            return []
        selected_rows = (
            sorted(rows, key=lambda row: _category_order(label(category(row))))
            if sort == "category"
            else ordered(rows, lambda row: value(row, measures[0]), asc=sort == "value_asc")
            if sort is not None
            else rows
        )
        categories = [label(category(row)) for row in selected_rows]
        values = [[value(row, column) for row in selected_rows] for column in measures[:3]]
        if not any(x is not None and y is not None for x, y in zip(values[0], values[1], strict=True)):
            return []
        shown = list(zip(measures[:3], values[:3], strict=True))
        chart = spec(
            "scatter",
            "multi_measure",
            dimension,
            categories,
            [series(column, items, mark="line", axis=index) for index, (column, items) in enumerate(shown)],
        )
        chart["points"] = [
            {
                "name": category_label(category),
                "x": values[0][index],
                "y": values[1][index],
                "size": values[2][index] if len(values) >= 3 else None,
            }
            for index, category in enumerate(chart["categories"])
        ]
        return [chart]

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

        def comparison_data(sort_column, columns):
            ranked = (
                sorted(rows, key=lambda row: _category_order(label(category(row))))
                if sort == "category"
                else ordered(rows, lambda row: value(row, sort_column), asc=sort == "value_asc")
            )
            kept, tail = ranked[:limit], ranked[limit:]
            categories = [label(category(row)) for row in kept]
            values = [[value(row, column) for row in kept] for column in columns]
            merged = None
            if top_n is not None and tail and all(additive(c, units) for c in columns):
                merged = {"count": len(tail)}
                categories.append(gettext("其他"))
                for column, items in zip(columns, values, strict=True):
                    items.append(_sum(value(row, column) for row in tail))
            return categories, values, merged, omitted(len(tail), merged)

        currents = [group[0] for group in groups]
        changes = [group[2] for group in groups]
        same_unit = (
            len({_unit_identity(units.get(c)) for c in currents}) == 1
            and len({_unit_identity(units.get(c)) for c in changes}) == 1
        )
        # 发散条形按涨跌着色，多条系列在同一张图里分不出属于哪个指标；多指标变化图每个指标一个面板
        # （multiples 每条 series 一个面板，也不会把不同量纲挤进同一坐标轴）。
        single = len(groups) == 1
        shown_groups = groups if single else groups[:MAX_PANELS]
        shown_changes = [group[2] for group in shown_groups]
        categories, values, merged, omission = comparison_data(changes[0], shown_changes)
        default = spec(
            "diverging_bar" if single else "multiples",
            "change",
            dimension,
            categories,
            [
                series(change, items, direction=current, axis=0 if single else index)
                for index, ((current, _, change, _), items) in enumerate(zip(shown_groups, values, strict=True))
            ],
            orientation="horizontal",
            others=merged,
            dropped=len(groups) - len(shown_groups),
            omitted_categories=omission,
        )
        change_options = [default] if not single or any(item is not None for item in values[0]) else []
        combo_categories, combo_values, combo_merged, combo_omission = comparison_data(measures[0], measures)
        extra = combo_candidate(dimension, combo_categories, combo_values)
        for chart in extra:
            chart["others"] = combo_merged
            chart["omitted_categories"] = combo_omission
        if groups:
            current, previous, change, _ = groups[0]
            pairs = [tuple(value(row, column) for column in (current, previous, change)) for row in rows]
            start = _sum(pair[1] for pair in pairs)
            end = _sum(pair[0] for pair in pairs)
            complete = all(
                all(item is not None for item in pair)
                and math.isclose(pair[0] - pair[1], pair[2], rel_tol=1e-9, abs_tol=1e-9)
                for pair in pairs
            )
            if (
                all(additive(column, units) for column in (current, previous, change))
                and complete
                and start is not None
                and end is not None
            ):
                members = defaultdict(list)
                for row in rows:
                    members[category(row)].append(row)
                changes_by_member = {
                    member: _sum(value(row, change) for row in items) for member, items in members.items()
                }
                ranked = ordered(
                    list(members),
                    lambda member: abs(changes_by_member[member]) if changes_by_member[member] is not None else None,
                )
                kept, tail = ranked[:limit], ranked[limit:]
                categories = [label(member) for member in kept]
                changes = [changes_by_member[member] for member in kept]
                if tail:
                    categories.append(gettext("其他"))
                    changes.append(_sum(changes_by_member[member] for member in tail))
                position = start
                for amount in changes:
                    if amount is None or not math.isfinite(position + amount):
                        complete = False
                        break
                    position += amount
                if complete and math.isclose(position, end, rel_tol=1e-9, abs_tol=1e-9):
                    waterfall = spec(
                        "waterfall",
                        "change",
                        dimension,
                        categories,
                        [series(change, changes, direction=current)],
                        others={"count": len(tail)} if tail else None,
                    )
                    waterfall["waterfall"] = {
                        "start": {"label": gettext(previous.rsplit("·", 1)[1]), "value": start},
                        "end": {"label": gettext("本期"), "value": end},
                    }
                    extra.append(waterfall)
        extra.extend(scatter_candidate())
        if not same_unit:
            return [*change_options, *extra]
        comparison_columns = [column for current, previous, _, _ in groups for column in (current, previous)]
        categories, values, merged, omission = comparison_data(currents[0], comparison_columns)
        candidate = spec(
            "grouped_bar",
            "comparison",
            dimension,
            categories,
            [
                series(
                    column,
                    items,
                    name=(period if len(groups) == 1 else f"{roles[current].get('label') or current} / {period}"),
                )
                for (current, previous, _, _), pair in zip(
                    groups, zip(values[::2], values[1::2], strict=True), strict=True
                )
                for column, items, period in (
                    (current, pair[0], gettext("本期")),
                    (previous, pair[1], gettext(previous.rsplit("·", 1)[1])),
                )
            ],
            orientation="horizontal",
            others=merged,
            omitted_categories=omission,
        )
        return [*change_options, candidate, *extra]

    def pivot(x_column, split, column):
        categories = list(dict.fromkeys(cell(row, x_column) for row in rows))
        keys = list(dict.fromkeys(split(row) for row in rows))
        cells = defaultdict(list)
        for row in rows:
            cells[cell(row, x_column), split(row)].append(value(row, column))
        values = {key: _sum(items) if additive(column, units) else items[0] for key, items in cells.items()}
        return categories, keys, values

    def multi(x, categories, values, *, temporal=False):
        same_unit = len({_unit_identity(units.get(c)) for c in measures}) == 1
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
                    for index, (column, items) in enumerate(
                        zip(measures[:MAX_PANELS], values[:MAX_PANELS], strict=True)
                    )
                ],
                dropped=max(0, len(measures) - MAX_PANELS),
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
            charts = [chart, candidate]
        else:
            chart = spec(
                "line" if temporal else "grouped_bar",
                reason,
                x,
                categories,
                [series(c, v, mark="line" if temporal else "bar") for c, v in zip(measures, values, strict=True)],
                orientation="vertical" if temporal else "horizontal",
            )
            charts = (
                [chart, alternate(chart, "bar" if len(measures) == 1 else "grouped_bar", "bar")]
                if temporal
                else [chart]
            )
        extra = area_candidate(charts) if temporal else []
        extra.extend(combo_candidate(x, categories, values))
        return [*charts, *extra]

    if time:
        periods = sorted(dict.fromkeys(cell(row, time) for row in rows), key=str)
        if len(periods) >= 2:
            if dimension:
                if len(measures) > 1:
                    # 现有分图契约每个系列一个面板；分别物化各指标，再按“指标 / 成员”分图。
                    charts = []
                    for column in measures:
                        selected_columns = [time, *dimensions, column]
                        selected_rows = [
                            [row[indices[c]] if indices[c] < len(row) else None for c in selected_columns]
                            for row in rows
                        ]
                        charts.append(
                            _recommend(selected_columns, selected_rows, roles, units, polarity, intent, top_n=top_n)[0]
                        )
                    # 沿用各指标内部的成员优先级，逐轮分配面板，先让每个指标都有展示机会。
                    candidates = [
                        (s, chart["others"]["count"] if s["other"] else 1)
                        for members in zip_longest(*(chart["series"] for chart in charts))
                        for chart, s in zip(charts, members, strict=True)
                        if s
                    ]
                    kept = candidates[:MAX_PANELS]
                    panels = [
                        {**s, "name": f"{s['label']} / {s['name']}", "axis": index} for index, (s, _) in enumerate(kept)
                    ]
                    # 聚合面板按其覆盖的原始成员数计数；截掉的“其他”不能再覆盖任何成员。
                    merged_count = sum(count for s, count in kept if s["other"])
                    result = [
                        spec(
                            "multiples",
                            "time_by_dimension",
                            time,
                            periods,
                            panels,
                            others={"count": merged_count} if merged_count else None,
                            dropped=sum(chart["dropped_series"] for chart in charts)
                            + sum(count for _, count in candidates[MAX_PANELS:]),
                        )
                    ]
                    extra = (
                        combo_candidate(time, periods, [chart["series"][0]["values"] for chart in charts])
                        if all(len(chart["series"]) == 1 for chart in charts)
                        else []
                    )
                    return [*result, *extra, *scatter_candidate()]
                column = measures[0]
                _, members, values = pivot(time, member_key, column)
                merged, dropped = None, 0
                selected = members
                if len(members) > 8:
                    ranked = ordered(
                        members, lambda member: abs(sum(values.get((period, member)) or 0 for period in periods))
                    )
                    selected = ranked[:7]
                    if additive(column, units):
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
                result = [chart, alternate(chart, "stacked_bar" if additive(column, units) else "grouped_bar", "bar")]
                extra = area_candidate(result)
                heatmap_periods = [
                    period
                    for period in periods
                    if period is not None and (not isinstance(period, str) or period.strip())
                ]
                heatmap_members = [
                    member for member in members if any((period, member) in values for period in heatmap_periods)
                ]
                if (
                    len(by_role["time"]) == len(dimensions) == 1
                    and len(heatmap_periods) >= 3
                    and len(heatmap_members) >= 2
                ):
                    ranked = ordered(
                        heatmap_members, lambda member: _sum(values.get((period, member)) for period in heatmap_periods)
                    )
                    selected_members = ranked[:12] if len(heatmap_members) > 12 else heatmap_members
                    cells = [
                        [x_index, y_index, values.get((period, member))]
                        for x_index, period in enumerate(heatmap_periods)
                        for y_index, member in enumerate(selected_members)
                    ]
                    present = [cell[2] for cell in cells if cell[2] is not None]
                    if present:
                        heatmap = spec(
                            "heatmap",
                            "time_by_dimension",
                            time,
                            heatmap_periods,
                            [series(column, [])],
                            dropped=max(0, len(heatmap_members) - 12),
                        )
                        heatmap["heatmap"] = {
                            "y": {"column": dimension, "label": roles[dimension].get("label") or dimension},
                            "y_categories": [category_label(label(member)) for member in selected_members],
                            "cells": cells,
                            "min": min(present),
                            "max": max(present),
                        }
                        extra.append(heatmap)
                return [*result, *extra]
            # 时间分组结果通常每期一行；同一格的可加值合计，不可加值保留首行。
            values = []
            for column in measures:
                cells = defaultdict(list)
                for row in rows:
                    cells[cell(row, time)].append(value(row, column))
                values.append([_sum(cells[p]) if additive(column, units) else cells[p][0] for p in periods])
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
            _sum(value(row, column) for row in items) if additive(column, units) else value(items[0], column)
            for column in measures
        ]
        for category, items in grouped.items()
    }
    categories = (
        sorted(grouped, key=lambda category: _category_order(label(category)))
        if sort == "category"
        else ordered(
            list(grouped),
            lambda category: values[category][0],
            asc=sort == "value_asc" or (sort is None and intent.get("order") == "asc"),
        )
    )
    tail = categories[limit:]
    categories = categories[:limit]
    materialized = [[values[category][index] for category in categories] for index in range(len(measures))]
    categories = [label(category) for category in categories]
    if len(measures) >= 2:
        result = multi(dimension, categories, materialized)
        for chart in result:
            if top_n is not None and tail and all(additive(s["column"], units) for s in chart["series"]):
                chart["others"] = {"count": len(tail)}
                chart["categories"] = [*categories, gettext("其他")]
                for s in chart["series"]:
                    index = measures.index(s["column"])
                    s["values"] = [*s["values"], _sum(values[category][index] for category in tail)]
            chart["omitted_categories"] = omitted(len(tail), chart["others"])
        return [*result, *scatter_candidate()]
    column = measures[0]
    merged = None
    if tail and additive(column, units):
        merged = {"count": len(tail)}
        categories.append(gettext("其他"))
        materialized[0].append(_sum(values[category][0] for category in tail))
    reason = "ranking" if intent.get("order") is not None or intent.get("limit") is not None else "composition"
    chart = spec(
        "bar",
        reason,
        dimension,
        categories,
        [series(column, materialized[0])],
        orientation="horizontal",
        others=merged,
        omitted_categories=omitted(len(tail), merged),
    )
    result = [chart]
    if additive(column, units) and len(categories) <= 8 and all(v is not None and v >= 0 for v in materialized[0]):
        result.append(alternate(chart, "donut", "bar"))
    if (
        additive(column, units)
        and len(grouped) >= 4
        and all(items[0] is not None and items[0] >= 0 for items in values.values())
    ):
        ranked = ordered(list(grouped), lambda category: values[category][0])
        kept, tail = ranked[:limit], ranked[limit:]
        categories = [label(category) for category in kept]
        amounts = [values[category][0] for category in kept]
        if tail:
            categories.append(gettext("其他"))
            amounts.append(_sum(values[category][0] for category in tail))
        pareto = spec(
            "pareto",
            "composition",
            dimension,
            categories,
            [series(column, amounts)],
            others={"count": len(tail)} if tail else None,
        )
        scale = max(values[category][0] for category in ranked)
        weights = [values[category][0] / scale if scale else 0.0 for category in ranked]
        shown_weights = weights[:limit]
        if tail:
            shown_weights.append(math.fsum(weights[limit:]))
        total = math.fsum(weights)
        running = 0
        cumulative = []
        for weight in shown_weights:
            running += weight
            cumulative.append(min(1.0, running / total) if total else 0.0)
        pareto["cumulative"] = cumulative
        result.append(pareto)
    return result
