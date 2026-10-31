"""分析评测的判分 v2.1：纯函数，不调用模型、不连接数据库，冻结的 v2.0 继续并列保留。

只修改以下两条，其余规则直接调用 scoring_v2：
1. 首要原因按机制匹配：来源资产及其视图上游、证据行的成员必须同时成立，或精确命中因子编码。
   编码与显示名来自生成器字典，避免 douyin 与抖音不一致，也避免 label 碰巧含关键字而得分。
   derive 展开实际选中的输入行（最多递归 6 层），所有行都必须属于备选成员，防止混入自然流量。
   类型、方向、核验、offsetting 的前两项及日历题继续由 v1 / v2 判定。
   禁止关键字与原因必含也展开同一字典，保证成员语义对正例、反例一致；实体文本仍用 v1 的取法。
2. 口径类关键数值可绑定带探索标记的数据：原值等于某期真值、实体齐全、该列的日期窗口完全一致。
   日期只用 sqlglot 的 starrocks 方言解析；WHERE、列内 CASE 与日期分组行共同限定窗口，缺界不猜。
   contribution 沿输入列检查两期，derive 仍拒绝；额外筛选只列明，不因缺少维度映射而判错。
"""

from __future__ import annotations

import ast
import re
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from tests.eval import scoring as v1
from tests.eval import scoring_v2 as v2

VERSION = "v2.1"
SPEC = Path(__file__).with_name("analysis_questions_v2_1.yaml")
GENERATOR = Path(__file__).resolve().parents[3] / "scripts" / "ec_data.py"


def load_questions(truth: dict, spec_path: Path = SPEC) -> dict[str, Any]:
    """调用者显式提供真值，避免默认加载器隐式访问演示数据。"""
    if not truth:
        raise ValueError("必须显式提供非空真值，不能回退到演示数据")
    data = v2.load_questions(truth=truth, spec_path=spec_path)
    for question in data["questions"]:
        question["v2_1"] = question["v2"]
    return data


@lru_cache(maxsize=1)
def generator_constants() -> dict:
    """只读取字面常量，不导入带灌数依赖的生成器模块。"""
    names = {"REGIONS", "CITIES", "PAID_CHANNELS", "VIEWS"}
    tree = ast.parse(GENERATOR.read_text(encoding="utf-8"))
    return {target.id: ast.literal_eval(node.value)
            for node in tree.body if isinstance(node, ast.Assign)
            for target in node.targets if isinstance(target, ast.Name) and target.id in names}


def aliases(value: Any) -> set[str]:
    """编码与名称双向展开；不引入逐题同义词。"""
    word = str(value)
    result = {word}
    constants = generator_constants()
    for name in ("REGIONS", "CITIES", "PAID_CHANNELS"):
        for code, label, *_ in constants[name]:
            if word in (code, label):
                result.update((code, label))
    return result


def sql_trees(sql: str, governed: bool = False) -> list[exp.Expression]:
    """治理 SQL 按注释分段，坏段跳过；探索 SQL 解析失败不换方言。"""
    parts = re.split(r"\n\s*--", sql) if governed else [sql]
    trees = []
    for i, part in enumerate(parts):
        # 分隔符吃掉了 --，补回后由解析器处理注释文字。
        if governed and i:
            part = "--" + part
        if not part.strip():
            continue
        try:
            trees.extend(t for t in sqlglot.parse(part, dialect="starrocks") if t is not None)
        except ParseError:
            if not governed:
                raise
    return trees


def table_names(tree: exp.Expression) -> set[str]:
    """仅保留库与表，catalog 不参与资产比较。"""
    return {f"{table.db}.{table.name}".lower() for table in tree.find_all(exp.Table) if table.db}


@lru_cache(maxsize=1)
def view_lineage() -> dict[str, set[str]]:
    """视图依赖的传递闭包；环路只访问一次。"""
    direct = {name.lower(): set().union(*(table_names(t) for t in sql_trees(sql)))
              for name, _comment, sql in generator_constants()["VIEWS"]}
    closure = {}
    for name in direct:
        seen, pending = set(), list(direct[name])
        while pending:
            item = pending.pop()
            if item not in seen:
                seen.add(item)
                pending.extend(direct.get(item, ()))
        closure[name] = seen
    return closure


def source_tables(run: dict, refs: list[str], depth: int = 0) -> set[str]:
    """计算结果沿 inputs 追溯；超过六层的来源不作推断。"""
    if depth > 6:
        return set()
    datasets = {d["ref"]: d for d in run.get("datasets") or []}
    tables = set()
    for ref in refs:
        ds = datasets.get(ref) or {}
        if ds.get("kind") == "calc":
            tables.update(source_tables(run, ds.get("inputs") or [], depth + 1))
        elif ds.get("kind") in ("explore", "governed"):
            for tree in sql_trees(ds.get("sql") or "", ds["kind"] == "governed"):
                tables.update(table_names(tree))
    return tables


def _rows(ds: dict) -> list[dict]:
    return [dict(zip(ds.get("columns") or [], row)) for row in ds.get("rows") or []]


def _select_rows(ds: dict, match: dict, exclude: bool = False) -> list[dict]:
    return [row for row in _rows(ds)
            if all(str(row.get(k)) == str(v) for k, v in match.items()) != bool(exclude)]


def evidence_rows(cause: dict, run: dict) -> tuple[list[dict], list[str], str | None]:
    """原因的成员证据与来源；证据行号从一开始，不能用 label 补足缺失行。"""
    entity = cause.get("entity") or {}
    if "member" in entity:
        return [{"member": entity["member"]}], list(cause.get("evidence") or []), None
    match = re.fullmatch(r"(D\d+)#(\d+)", str(entity.get("evidence_id") or ""))
    if not match:
        return [], [], "原因没有成员或证据行"
    ref, n = match[1], int(match[2])
    datasets = {d["ref"]: d for d in run.get("datasets") or []}
    rows = _rows(datasets.get(ref) or {})
    if not 1 <= n <= len(rows):
        return [], [ref], "证据行不在保存的行里"
    return _expand_evidence(ref, [rows[n - 1]], run, 0)


def _expand_evidence(ref: str, rows: list[dict], run: dict, depth: int) -> tuple[list[dict], list[str], str | None]:
    if depth > 6:
        return [], [], "证据展开超过深度上限 6"
    datasets = {d["ref"]: d for d in run.get("datasets") or []}
    step = next((s for s in run.get("steps") or [] if s.get("dataset") == ref), {})
    args = step.get("args") or {}
    if step.get("tool") == "derive":
        expanded, sources = [], []
        for value in args.get("values") or []:
            source = value.get("dataset")
            selected = _select_rows(datasets.get(source) or {}, value.get("match") or {}, value.get("exclude", False))
            if not selected:
                return [], [], "派生值的输入在保存的行里没有选中行"
            part, refs, why = _expand_evidence(source, selected, run, depth + 1)
            if why:
                return [], [], why
            expanded.extend(part)
            sources.extend(refs)
        return expanded, list(dict.fromkeys(sources)), None
    sources = [ref]
    if step.get("tool") == "contribution" and args.get("dataset"):
        sources.append(args["dataset"])
    return rows, sources, None


def match_mechanism(cause: dict, run: dict, alternatives: list[dict]) -> dict:
    """命中任一备选即可，备选序号从一开始。"""
    rows, refs, why = evidence_rows(cause, run)
    tables = None
    misses = []
    for i, option in enumerate(alternatives, 1):
        if "factor" in option:
            hit = (cause.get("entity") or {}).get("factor_metric_code") == option["factor"]
            reason = None if hit else "因子编码不匹配"
        else:
            if tables is None:
                tables = source_tables(run, refs)
                tables |= set().union(*(view_lineage().get(t, set()) for t in list(tables)))
            members = set().union(*(aliases(v) for v in option.get("members") or []))
            asset_ok = option["asset"].lower() in tables
            member_ok = bool(rows) and all(any(str(cell) in members for cell in row.values()) for row in rows)
            hit = not why and asset_ok and member_ok
            reason = why or ("来源资产及其上游不匹配" if not asset_ok else "证据行的成员不匹配")
        if hit:
            return {"hit": True, "alternative": i, "option": option, "sources": refs,
                    "tables": sorted(tables or []), "rows": rows}
        misses.append({"alternative": i, "reason": reason})
    return {"hit": False, "reason": "；".join(dict.fromkeys(m["reason"] for m in misses)), "misses": misses}


def _unwrap(node: exp.Expression) -> exp.Expression:
    while isinstance(node, (exp.Cast, exp.Paren, exp.Alias)):
        node = node.this
    return node


def _date_literal(node: exp.Expression | None) -> date | None:
    if node is None:
        return None
    node = _unwrap(node)
    if not isinstance(node, exp.Literal) or not node.is_string:
        return None
    try:
        return date.fromisoformat(node.this)
    except ValueError:
        return None


def _intersect(left: tuple, right: tuple) -> tuple:
    lows = [v for v in (left[0], right[0]) if v is not None]
    highs = [v for v in (left[1], right[1]) if v is not None]
    return max(lows) if lows else None, min(highs) if highs else None


def _date_bounds(condition: exp.Expression | None) -> dict[str, tuple]:
    """按日期列表达式分别求界；OR 不能当 AND 收窄窗口。"""
    if condition is None:
        return {}
    condition = _unwrap(condition)
    if isinstance(condition, exp.And):
        bounds = _date_bounds(condition.this)
        for key, span in _date_bounds(condition.expression).items():
            bounds[key] = _intersect(bounds.get(key, (None, None)), span)
        return bounds
    if isinstance(condition, exp.Between):
        low, high = _date_literal(condition.args.get("low")), _date_literal(condition.args.get("high"))
        field = _unwrap(condition.this)
        if low and high and list(field.find_all(exp.Column)):
            return {field.sql(dialect="starrocks"): (low, high)}
        return {}
    if not isinstance(condition, (exp.GTE, exp.GT, exp.LTE, exp.LT, exp.EQ)):
        return {}
    left, right = _unwrap(condition.this), _unwrap(condition.expression)
    stamp, field, kind = _date_literal(right), left, type(condition)
    if stamp is None:
        stamp, field = _date_literal(left), right
        kind = {exp.GTE: exp.LTE, exp.GT: exp.LT, exp.LTE: exp.GTE, exp.LT: exp.GT, exp.EQ: exp.EQ}[kind]
    if stamp is None or not list(field.find_all(exp.Column)):
        return {}
    low = stamp + timedelta(days=1) if kind is exp.GT else stamp if kind in (exp.GTE, exp.EQ) else None
    high = stamp - timedelta(days=1) if kind is exp.LT else stamp if kind in (exp.LTE, exp.EQ) else None
    return {field.sql(dialect="starrocks"): (low, high)}


def _where_values(condition: exp.Expression | None) -> set[str]:
    """只接受合取中的等号及单值 IN，避免把候选集合误当实体筛选。"""
    if condition is None:
        return set()
    condition = _unwrap(condition)
    if isinstance(condition, exp.And):
        return _where_values(condition.this) | _where_values(condition.expression)
    if isinstance(condition, exp.EQ):
        for field, value in ((condition.this, condition.expression), (condition.expression, condition.this)):
            value = _unwrap(value)
            if isinstance(_unwrap(field), exp.Column) and isinstance(value, exp.Literal) and not _date_literal(value):
                return {str(value.this)}
    if isinstance(condition, exp.In) and isinstance(_unwrap(condition.this), exp.Column):
        values = condition.expressions
        if len(values) == 1 and isinstance(_unwrap(values[0]), exp.Literal) and not _date_literal(values[0]):
            return {str(_unwrap(values[0]).this)}
    return set()


def _projection(tree: exp.Select, column: str) -> exp.Expression | None:
    return next((item for item in tree.expressions if item.alias_or_name == column), None)


def _group_bounds(tree: exp.Select, row: dict) -> dict[str, tuple]:
    bounds = {}
    group = tree.args.get("group")
    for expression in group.expressions if group else []:
        node = _unwrap(expression)
        if isinstance(node, exp.Column):
            projected = _projection(tree, node.name)
            if projected is not None:
                node = _unwrap(projected)
        if isinstance(node, exp.Literal) and not node.is_string:
            position = int(node.this) - 1
            if not 0 <= position < len(tree.expressions):
                continue
            node = _unwrap(tree.expressions[position])
        outputs = [item.alias_or_name for item in tree.expressions if _unwrap(item) == node]
        raw = next((row[name] for name in outputs if name in row), None)
        if raw is None:
            continue
        text = str(raw)
        try:
            stamp = date.fromisoformat(text[:10] if len(text) >= 10 else text + "-01")
        except ValueError:
            continue
        if isinstance(node, (exp.TimestampTrunc, exp.DateTrunc)):
            if str(node.args.get("unit")).strip("'").lower() != "month":
                continue
            first = stamp.replace(day=1)
            following = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
            bounds[_unwrap(node.this).sql(dialect="starrocks")] = (first, following - timedelta(days=1))
        elif isinstance(node, exp.Column):
            bounds[node.sql(dialect="starrocks")] = (stamp, stamp)
    return bounds


def column_window(tree: exp.Select, column: str, row: dict) -> tuple[tuple | None, str | None]:
    """WHERE 与数值列内 CASE 的交集，再与选中日期分组行相交。"""
    expression = _projection(tree, column)
    if expression is None:
        return None, "窗口不确定：数值列没有对应的 SELECT 表达式"
    return _expression_window(tree, expression, row)


def _expression_window(tree: exp.Select, expression: exp.Expression, row: dict) -> tuple[tuple | None, str | None]:
    where = tree.args.get("where")
    common = _date_bounds(where.this if where else None)
    cases = list(expression.find_all(exp.Case))
    # CASE 外的度量或非零 ELSE 仍会读取其他日期，不能用 WHEN 缩窄整列表达式。
    outside = any(column.find_ancestor(exp.Case) is None for column in expression.find_all(exp.Column))
    defaults = [case.args.get("default") for case in cases]
    closed = all(value is None or isinstance(value, exp.Null)
                 or (isinstance(value, exp.Literal) and not value.is_string and v1._number(value.this) == 0)
                 for value in defaults)
    branches = ([item.this for case in cases for item in case.args.get("ifs") or []]
                if closed and not outside else [])
    # 多个日期分支只有窗口一致时才可合并；不能把相互排斥的月份取交集。
    contexts = []
    for condition in branches or [None]:
        bounds = dict(common)
        for key, span in _date_bounds(condition).items():
            bounds[key] = _intersect(bounds.get(key, (None, None)), span)
        for key, span in _group_bounds(tree, row).items():
            bounds[key] = _intersect(bounds.get(key, (None, None)), span)
        if len(bounds) != 1:
            return None, "窗口不确定：无法确定唯一的日期列"
        span = next(iter(bounds.values()))
        if None in span or span[0] > span[1]:
            return None, "窗口不确定"
        contexts.append(span)
    if any(span != contexts[0] for span in contexts):
        return None, "窗口不确定：列包含不同日期窗口"
    return tuple(str(day) for day in contexts[0]), None


def _selected_row(ds: dict, selector: str | None) -> tuple[dict | None, str | None]:
    if selector and selector.startswith("#"):
        if not selector[1:].isdigit() or not 1 <= int(selector[1:]) <= len(ds.get("rows") or []):
            return None, "证据行不在保存的行里"
    return v2.RunIndex._row(ds, selector)


def _union_spans(spans: list[tuple]) -> tuple[tuple | None, str | None]:
    """贡献计算可以汇总同组多行，但日期有缺口时不能冒充完整期间。"""
    ordered = sorted(set(spans))
    first, last = ordered[0]
    for start, end in ordered[1:]:
        if date.fromisoformat(start) > date.fromisoformat(last) + timedelta(days=1):
            return None, "窗口不确定：contribution 输入日期有缺口"
        last = max(last, end)
    return (first, last), None


def _explore_context(index: v2.RunIndex, ref: str, column: str, row: dict,
                     depth: int = 0) -> tuple[dict | None, str | None]:
    if depth > 6:
        return None, "探索来源追溯超过深度上限 6"
    ds = index.datasets.get(ref) or {}
    step = index.producer.get(ref) or {}
    args = step.get("args") or {}
    if step.get("tool") == "derive" or ds.get("title", "").startswith("派生："):
        return None, "派生计算（derive），公式语义不能自动绑定"
    if step.get("tool") == "contribution":
        period = {"previous": "compare", "current": "current", "change": "change"}.get(column)
        if period is None:
            return None, "contribution 引用不是两期值或变化量"
        source = args.get("dataset")
        source_ds = index.datasets.get(source) or {}
        match = {key: row[key] for key in args.get("keys") or [] if key in row}
        if len(match) != len(args.get("keys") or []):
            return None, "contribution 行缺少 keys 的取值"
        selected = _select_rows(source_ds, match)
        if not selected:
            return None, "contribution 在保存的输入行里没有选中行"
        periods = ("current", "compare") if period == "change" else (period,)
        spans, values, filters = [], set(), set()
        for part in periods:
            input_col = args.get("previous" if part == "compare" else "current")
            part_spans = []
            for selected_row in selected:
                context, why = _explore_context(index, source, input_col, selected_row, depth + 1)
                if why:
                    return None, why
                if len(context["span"]) != 1:
                    return None, "窗口不确定：contribution 的输入列不是单期值"
                part_spans.extend(context["span"])
                values.update(context["values"])
                filters.update(context["filters"])
            combined, why = _union_spans(part_spans)
            if why:
                return None, why
            spans.append(combined)
        values.update(str(v) for v in match.values())
        return {"source": "explore", "period": period, "span": tuple(spans), "values": values,
                "filters": filters, "calc": "delta" if period == "change" else "level"}, None
    trees = sql_trees(ds.get("sql") or "", ds.get("kind") == "governed")
    selects = [tree for tree in trees if isinstance(tree, exp.Select) and _projection(tree, column) is not None]
    if len(selects) != 1:
        return None, "窗口不确定：无法唯一确定数值列的查询"
    tree = selects[0]
    expression = _unwrap(_projection(tree, column))
    # 明确的两期差额分别保留左右窗口；其他多窗口表达式仍不猜计算语义。
    delta = isinstance(expression, exp.Sub) and all(
        list(part.find_all(exp.Case)) for part in (expression.this, expression.expression))
    spans = []
    for part in (expression.this, expression.expression) if delta else (expression,):
        span, why = _expression_window(tree, part, row)
        if why:
            return None, why
        spans.append(span)
    where = tree.args.get("where")
    filters = _where_values(where.this if where else None)
    return {"source": "explore", "span": tuple(spans), "values": filters | {str(v) for v in row.values()},
            "filters": filters, "calc": "delta" if delta else "level", **({"period": "change"} if delta else {})}, None


def _is_explore(index: v2.RunIndex, ref: str, depth: int = 0) -> bool:
    if depth > 6:
        return False
    ds = index.datasets.get(ref) or {}
    if ds.get("kind") == "explore":
        return True
    step = index.producer.get(ref) or {}
    return step.get("tool") == "contribution" and _is_explore(index, (step.get("args") or {}).get("dataset"), depth + 1)


def explore_binding(q: dict, run: dict, segment: dict, spec: dict,
                    periods: list[str]) -> tuple[dict | None, str | None]:
    """数值已匹配真值后，检查探索语境的实体与精确期间。"""
    index = v2.RunIndex(run)
    path = v2.PATH.match(segment.get("path") or "")
    if path is None:
        return None, "引用路径无法解析"
    ds = index.datasets.get(path["ds"])
    if ds is None:
        return None, "数据集不在运行记录里"
    row, why = _selected_row(ds, path["sel"])
    if why:
        return None, why
    if row is None:
        return None, "探索数据集没有选定唯一行"
    context, why = _explore_context(index, path["ds"], path["col"], row)
    if why:
        return None, why
    used = set()
    for value in spec.get("entity", {}).values():
        matched = aliases(value) & context["values"]
        if not matched:
            return None, f"探索语境缺少实体 {value}"
        used.update(aliases(value))
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
    """受治理及表列类绑定直接复用 v2，只对口径类探索引用增加候选。"""
    baseline = v2.key_value(q, report, run)
    if baseline["hit"] is not False:
        return baseline
    key = (q.get("expect") or {}).get("key_value") or {}
    index, misses = v2.RunIndex(run), []
    tolerance = Decimal(str(key.get("tolerance", 0)))
    for segment in v2._value_segments(report):
        path = v2.PATH.match(segment.get("path") or "")
        if path is None or not (segment.get("explore") or _is_explore(index, path["ds"])):
            continue
        raw = v1._number(segment.get("raw"))
        if raw is None:
            continue
        for spec in (q.get("v2") or {}).get("key_bindings") or []:
            if not spec.get("metric") or not spec.get("truth"):
                continue
            periods = [period for period, value in spec["truth"].items()
                       if v1._within(abs(raw) if key.get("abs") else raw,
                                     abs(Decimal(str(value))) if key.get("abs") else Decimal(str(value)), tolerance)]
            if not periods:
                continue
            binding, why = explore_binding(q, run, segment, spec, periods)
            if binding is not None:
                return {"hit": True, "path": segment.get("path"), "binding": v2._plain(binding)}
            misses.append(f"{segment.get('path')}：数值相同但{why}")
    if misses:
        return {"hit": False, "reason": "；".join(dict.fromkeys(misses))[:1200]}
    return baseline


def score(question: dict, run: dict | None) -> dict[str, Any]:
    """只替换机制、成员别名与探索数值，其余完成、政策、业务与权限判分沿用 v2。"""
    adapted = deepcopy(question)
    expect = adapted.setdefault("expect", {})
    report = (run or {}).get("report") or {}
    causes = report.get("causes") or []
    original_forbidden = expect.get("forbidden") or []
    expect["forbidden"] = sorted(set().union(*(aliases(word) for word in original_forbidden)))
    verified = [v1._entity_text(cause) for cause in causes if cause.get("status") == "verified"]
    if expect.get("required_in_causes"):
        expect["required_in_causes"] = [next((alias for alias in sorted(aliases(word))
                                               if any(alias in text for text in verified)), word)
                                        for word in expect["required_in_causes"]]
    primary = expect.get("primary")
    options = (question.get("v2_1") or question.get("v2") or {}).get("mechanism") or question.get("mechanism")
    detail = {"hit": None, "reason": "题目没有机制定义，沿用 v1"}
    if primary and primary.get("entity") and options:
        heads = ([c for c in causes[:2] if c.get("direction") == primary["direction"]]
                 if report.get("conclusion") == "offsetting" else causes[:1])
        detail = (match_mechanism(heads[0], run or {}, options) if heads else
                  {"hit": False, "reason": "没有方向相同的首要原因"})
        primary["entity"] = []
        if not detail["hit"]:
            primary["types"] = []
    # 让 v2 完整执行其余门槛，避免复制权限拒绝、业务前提等冻结规则。
    expect.pop("key_value", None)
    result = v2.score(adapted, run)
    result["scorer"] = VERSION
    result["forbidden"] = sorted({word for word in original_forbidden if any(
        alias in v1._entity_text(cause) for cause in causes for alias in aliases(word))})
    if options and primary and detail["hit"] and not result["primary_hit"]:
        detail = {**detail, "hit": False, "reason": "机制匹配，但运行完成、类型、方向或核验条件不满足"}
    if detail["hit"] is None:
        detail = {**detail, "hit": result["primary_hit"]}
    result["primary_detail"] = result["mechanism"] = detail
    key = (question.get("expect") or {}).get("key_value")
    kv = key_value(question, report, run or {}) if result["completed"] else {
        "hit": False if key else None, "reason": "运行没有完成"}
    result["key_value_hit"], result["key_value_detail"] = kv["hit"], kv
    if question["category"] in v1.POSITIVE or (question["category"] == "adversarial" and primary):
        result["passed"] = bool(result["passed"] and kv["hit"] is not False)
    return result
