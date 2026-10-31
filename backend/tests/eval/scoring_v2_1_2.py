"""分析评测的判分 v2.1.2：题目规格及其余规则沿用 v2.1.1。

相对 v2.1.1 只修正以下六处（PR #11 评审线程）：
1. 4430232921：contribution 的每个输入行都要满足实体，避免跨实体合计碰巧等于真值而误通过。
2. 4426619623：explain_change 的 summary.total 按整次归因绑定，不要求选中成员行。
3. 4426775415：治理列的“·上一期”“·去年同期”与“·对比期”同义，沿用原来的期间计算。
4. 4426775439：factor_decompose 的 match 属于所选实体，不能在因子绑定时丢失。
5. 4426832504：治理时间桶与查询窗口相交后可覆盖整个题目期间，不能一律拒绝日 / 月分组。
6. 4427578578：explain 的复合成员按 × 维度及空格斜杠拆分，与 v2.1.1 的机制匹配保持一致。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from tests.eval import scoring as v1
from tests.eval import scoring_v2 as v2
from tests.eval import scoring_v2_1 as v21
from tests.eval import scoring_v2_1_1 as v211
from tests.eval.scoring_v2_1_1 import SPEC as SPEC
from tests.eval.scoring_v2_1_1 import load_questions as load_questions

VERSION = "v2.1.2"


class RunIndex(v2.RunIndex):
    """只覆盖六处修正涉及的绑定，其余来源与口径追溯继续用 v2。"""

    def bind(self, segment: dict, window: dict | None = None) -> tuple[dict | None, str | None]:
        path = v2.PATH.match(segment.get("path") or "")
        if path is None or path["ds"] not in self.datasets:
            return super().bind(segment)
        ref, col, sel = path["ds"], path["col"], path["sel"]
        ds = self.datasets[ref]
        step = self.producer.get(ref) or {}
        tool = step.get("tool")
        if ds["kind"] == "explore" or tool == "derive" or ds.get("title", "").startswith("派生："):
            return super().bind(segment)
        ctx = self.context(ref)
        if ctx is None:
            return super().bind(segment)
        entity = {}
        for f in ctx.get("filters") or []:
            values = sorted({str(v) for v in f.get("values") or []})
            entity[f["dimension"]] = values[0] if len(values) == 1 else tuple(values)
        unit = segment.get("unit")
        total_period = {"summary.total.current": "current", "summary.total.previous": "compare",
                        "summary.total.change": "change"}.get(col)
        if tool == "explain_change" and total_period:
            return {"source": "explain", "metric": ctx.get("metric"), "entity": entity,
                    "calc": "delta" if total_period == "change" else "level", "unit": unit,
                    "span": self._span(ctx, total_period), "period": total_period}, None
        if ds["kind"] == "governed":
            row, why = self._row(ds, sel)
            if why and not col.startswith("summary."):
                return None, why
            for group in ctx.get("group_by") or []:
                if row is None:
                    return None, f"{ref} 按 {group} 分组但没有选定行"
                entity[group] = str(row.get(group))
            base, period = col, "current"
            for suffix, candidate in {**v2.SUFFIX, "·上一期": "compare", "·去年同期": "compare"}.items():
                if col.endswith(suffix):
                    base, period = col[:-len(suffix)], candidate
                    break
            spans = self._span(ctx, period)
            if ctx.get("time_grain"):
                # 产品的时间列名为 period；月桶支持日期及 YYYY-MM 两种保存格式。
                raw = str((row or {}).get("period") or "")
                try:
                    text = raw + "-01" if ctx["time_grain"] == "月" and len(raw) == 7 else raw[:10]
                    stamp = date.fromisoformat(text)
                except ValueError:
                    return None, f"{ref} 按{ctx['time_grain']}分组但没有选定有效的时间桶"
                first, last = stamp, stamp
                if ctx["time_grain"] == "月":
                    first = stamp.replace(day=1)
                    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
                bucket = (str(first), str(last))
                spans = tuple((max(span[0], bucket[0]), min(span[1], bucket[1])) if span else None
                              for span in spans)
                # 时间分组不能同时 compare，对比期通常独立查询；只按完整交集识别它的期间。
                if (period == "current" and window
                        and spans == ((window["compare_start"], window["compare_end"]),)
                        and spans != ((window["start"], window["end"]),)):
                    period = "compare"
            return {"source": "governed", "metric": ctx["names"].get(base) or self.names.get(base),
                    "entity": entity, "calc": {"current": "level", "compare": "level", "change": "delta",
                                              "rate": "rate"}[period], "unit": unit,
                    "span": spans, "period": period}, None
        got, why = super().bind(segment)
        if got is not None and got.get("source") == "factor" and tool == "factor_decompose":
            got["entity"].update({str(k): str(v) for k, v in ((step.get("args") or {}).get("match") or {}).items()})
        if got is not None and got.get("source") == "explain":
            row, _ = self._row(ds, sel)
            dimension, member = (row or {}).get("dimension"), (row or {}).get("member")
            if isinstance(dimension, str) and "×" in dimension and isinstance(member, str):
                parts = member.split(" / ")
                if len(parts) == len(dimension.split("×")):
                    got["entity"].pop(dimension, None)
                    got["entity"].update(zip(dimension.split("×"), parts))
        return got, why


def _explore_context(index: v2.RunIndex, ref: str, column: str, row: dict, entity: dict,
                     depth: int = 0) -> tuple[dict | None, str | None]:
    """贡献计算逐输入行核对实体；其他探索数据沿用 v2.1 的语境与日期解析。"""
    if depth > 6:
        return None, "探索来源追溯超过深度上限 6"
    ds = index.datasets.get(ref) or {}
    step = index.producer.get(ref) or {}
    args = step.get("args") or {}
    if step.get("tool") != "contribution" or ds.get("title", "").startswith("派生："):
        return v21._explore_context(index, ref, column, row, depth)
    period = {"previous": "compare", "current": "current", "change": "change"}.get(column)
    if period is None:
        return None, "contribution 引用不是两期值或变化量"
    source = args.get("dataset")
    source_ds = index.datasets.get(source) or {}
    match = {key: row[key] for key in args.get("keys") or [] if key in row}
    if len(match) != len(args.get("keys") or []):
        return None, "contribution 行缺少 keys 的取值"
    selected = v21._select_rows(source_ds, match)
    if not selected:
        return None, "contribution 在保存的输入行里没有选中行"
    periods = ("current", "compare") if period == "change" else (period,)
    spans, values, filters = [], set(), set()
    for part in periods:
        input_col = args.get("previous" if part == "compare" else "current")
        part_spans = []
        for selected_row in selected:
            context, why = _explore_context(index, source, input_col, selected_row, entity, depth + 1)
            if why:
                return None, why
            if any(not (v21.aliases(value) & context["values"]) for value in entity.values()):
                return None, "contribution 聚合了不属于该实体的行"
            if len(context["span"]) != 1:
                return None, "窗口不确定：contribution 的输入列不是单期值"
            part_spans.extend(context["span"])
            values.update(context["values"])
            filters.update(context["filters"])
        combined, why = v21._union_spans(part_spans)
        if why:
            return None, why
        spans.append(combined)
    values.update(str(v) for v in match.values())
    return {"source": "explore", "period": period, "span": tuple(spans), "values": values,
            "filters": filters, "calc": "delta" if period == "change" else "level"}, None


def explore_binding(q: dict, run: dict, segment: dict, spec: dict,
                    periods: list[str]) -> tuple[dict | None, str | None]:
    """沿用 v2.1 的探索候选门槛，只将 contribution 语境改成逐行实体检查。"""
    index = RunIndex(run)
    path = v2.PATH.match(segment.get("path") or "")
    if path is None:
        return None, "引用路径无法解析"
    ds = index.datasets.get(path["ds"])
    if ds is None:
        return None, "数据集不在运行记录里"
    row, why = v21._selected_row(ds, path["sel"])
    if why:
        return None, why
    if row is None:
        return None, "探索数据集没有选定唯一行"
    context, why = _explore_context(index, path["ds"], path["col"], row, spec.get("entity") or {})
    if why:
        return None, why
    used = set()
    for value in spec.get("entity", {}).values():
        if not v21.aliases(value) & context["values"]:
            return None, f"探索语境缺少实体 {value}"
        used.update(v21.aliases(value))
    unit = v2._units().get(spec["metric"])
    if unit and segment.get("unit") not in (None, "", unit):
        return None, f"单位是 {segment.get('unit')}，不是 {unit}"
    if spec.get("calc") and context["calc"] != spec["calc"]:
        return None, f"计算类型是 {context['calc']}，不是 {spec['calc']}"
    window = q["window"]
    current = (window["start"], window["end"])
    compare = (window["compare_start"], window["compare_end"])
    expected = {"current": (current,), "compare": (compare,), "change": (current, compare)}
    for period in periods:
        if period in expected and context.get("period", period) == period and context["span"] == expected[period]:
            return {"source": "explore", "metric": spec["metric"], "period": period, "span": context["span"],
                    "entity": spec.get("entity", {}), "calc": context["calc"], "unit": segment.get("unit"),
                    "extra_filters": sorted(context["filters"] - used)}, None
    if any(start == end for start, end in context["span"]) and any(
            start != end for period in periods for start, end in expected.get(period, ())):
        return None, "只绑定到单日的行，题目问的是整个期间"
    return None, f"探索列窗口 {context['span']} 与数值对应的题目期间不一致"


def key_value(q: dict, report: dict, run: dict) -> dict[str, Any]:
    """用新的 RunIndex 组合 v2 的数值门槛，再接上修正后的 v2.1 探索候选。"""
    key = (q.get("expect") or {}).get("key_value")
    bindings = (q.get("v2") or {}).get("key_bindings") or []
    if not key:
        return {"hit": None, "reason": "题目没有关键数值"}
    if any(b.get("manual") for b in bindings):
        return {"hit": None, "reason": "关键数值需人工审定（key_bindings.manual）"}
    if not bindings:
        return {"hit": None, "reason": "缺少 v2 绑定定义"}
    tolerance = Decimal(str(key.get("tolerance", 0)))
    index, misses, explore_candidates = RunIndex(run), [], []
    for segment in v2._value_segments(report):
        raw = v1._number(segment.get("raw"))
        if raw is None:
            continue
        got, why = index.bind(segment, q["window"])
        for spec in bindings:
            candidates = spec["truth"] if spec.get("truth") else {"value": Decimal(str(spec["value"]))}
            matched = [p for p, value in candidates.items()
                       if v1._within(abs(raw) if key.get("abs") else raw,
                                     abs(Decimal(str(value))) if key.get("abs") else Decimal(str(value)), tolerance)]
            if not matched:
                continue
            if got is None:
                misses.append(f"{segment.get('path')}：数值相同但{why}")
            elif spec.get("truth") and got.get("source") != "explore" and got.get("period") not in matched:
                misses.append(f"{segment.get('path')}：数值等于真值的{'、'.join(matched)}，"
                              f"但所在列是 {got.get('period')}")
            else:
                problem = v2._binding_ok(spec, got, q)
                if problem is None:
                    return {"hit": True, "path": segment.get("path"), "binding": v2._plain(got)}
                misses.append(f"{segment.get('path')}：数值相同但{problem}")
            path = v2.PATH.match(segment.get("path") or "")
            if (spec.get("metric") and spec.get("truth") and path is not None
                    and (segment.get("explore") or v21._is_explore(index, path["ds"]))):
                explore_candidates.append((segment, spec, matched))
    explore_misses = []
    for segment, spec, periods in explore_candidates:
        binding, why = explore_binding(q, run, segment, spec, periods)
        if binding is not None:
            return {"hit": True, "path": segment.get("path"), "binding": v2._plain(binding)}
        explore_misses.append(f"{segment.get('path')}：数值相同但{why}")
    return {"hit": False, "reason": "；".join(dict.fromkeys(explore_misses or misses))[:1200]
            or "报告里没有等于真值的数值引用"}


def score(question: dict, run: dict | None) -> dict[str, Any]:
    """完成、机制、权限及业务门槛沿用 v2.1.1，仅接入新的关键数值绑定。"""
    adapted = deepcopy(question)
    adapted.setdefault("expect", {}).pop("key_value", None)
    result = v211.score(adapted, run)
    result["scorer"] = VERSION
    key = (question.get("expect") or {}).get("key_value")
    kv = key_value(question, (run or {}).get("report") or {}, run or {}) if result["completed"] else {
        "hit": False if key else None, "reason": "运行没有完成"}
    result["key_value_hit"], result["key_value_detail"] = kv["hit"], kv
    primary = (question.get("expect") or {}).get("primary")
    if result["completed"] and (question["category"] in v1.POSITIVE
                                or (question["category"] == "adversarial" and primary)):
        result["passed"] = bool(result["passed"] and kv["hit"] is not False)
    return result
