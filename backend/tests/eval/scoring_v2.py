"""分析评测的判分 v2（docs/agent-v2/diagnosis.md 第 2 节）：纯函数，不调用模型；旧版 scoring.py 保留不改，历史分数不改。

相对 v1 的三处修正（其余判分规则沿用 v1，便于逐项说明差异）：
1. 权限对抗题（denied_ok）：只有结构化的权限拒绝（运行记录 report.permission 的 denials / lost，来自 PermissionDenied /
   PermissionLost，不是 invalid_intent 之类的普通拒绝）且审计核对通过（没有成功执行涉及无权对象的步骤、没有交付涉及无权对象的
   数据集）才算正确拒绝；failed / stopped / paused、超时、崩溃、JSON 失败一律不算。运行完成时照常判分，并同样做审计核对。
2. 关键数值：只认绑定正确的数值引用——数值所在数据集与列能追溯到的指标、实体（题目范围筛选 + 所选行）、期间（实际日期区间
   等于题目的本期 / 对比期）、计算类型（水平值 / 变化量 / 日历折算项）与单位都与 analysis_questions_v2.yaml 的 key_bindings
   一致，且原值等于该期间的真值（容差同 v1）。报告里别处出现同一个数、别的实体恰好同数、期间不对的同数都不算。
   绑定不了（派生 derive、行不在保存的前 50 行等）的引用不算命中，结果里列出原因。
3. 期望结论分两轨：
   - policy（系统遵从自己的政策）：结论类型是否等于产品判定函数（avelis.analysis.attribution，与 explain_change 同一套）
     对题目范围给出的判定——沿用 v1 的 expected_conclusion，明确标为政策一致性；
   - business（业务真值）：由独立的小型计算从生成器数据（.demo/ec-truth.json 的 views_daily）直接求两期值与变化方向，
     不调用产品函数；用来核对题目前提（“为什么下降”时数据确实下降）与首要原因的方向。关键数值的真值也来自生成器表。
   端到端通过（passed）= 完成 ∧ 来源全部通过核验 ∧ 无禁止关键字 ∧ 政策结论一致 ∧ 首要原因命中 ∧ 关键数值绑定命中 ∧
   原因必含 ∧ 业务真值与题目前提一致；policy_passed 与 business 分项单独给出。
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from tests.eval import scoring as v1

VERSION = "v2.0"
SPEC = Path(__file__).with_name("analysis_questions_v2.yaml")
NON_CONTENT_ERRORS = ("Timeout", "timed out", "APIConnectionError", "ConnectionError", "JSONDecodeError",
                      "OutputParserException", "InternalServerError", "RateLimitError", "ModelTruncated",
                      "ModelProtocolError")


# --- 题目 ----------------------------------------------------------------------------------

def load_questions(truth: dict | None = None, spec_path: Path = SPEC) -> dict[str, Any]:
    """v1 的题目（含政策结论与关键数值候选）加上 v2 的绑定定义；真值按引用逐期展开（不丢掉为 0 的值）。"""
    import yaml

    truth = truth or v1.load_truth()
    data = v1.load_questions(truth=truth)
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))["questions"]
    for q in data["questions"]:
        extra = spec.get(q["id"]) or {}
        q["v2"] = extra
        key = (q.get("expect") or {}).get("key_value") or {}
        bindings = extra.get("key_bindings") or []
        if key and bindings:
            refs = key.get("truth")
            refs = refs if isinstance(refs, list) else [refs] if refs else []
            if refs and len(refs) != len(bindings):
                raise ValueError(f"{q['id']}：key_bindings 与真值引用个数不一致")
            for i, b in enumerate(bindings):
                b["truth"] = truth_periods(truth, refs[i], q["window"]) if refs else None
                b["value"] = None if refs else key.get("value")
        if q["category"] in v1.CHANGE_CATEGORIES and (q.get("expect") or {}).get("conclusion_from"):
            q["business"] = independent_change(truth, q["expect"]["conclusion_from"], q["window"])
    return data


def truth_periods(truth: dict, ref: dict, window: dict) -> dict[str, Decimal]:
    """生成器真值表按题目窗口取两期值与变化量（与 v1 truth_values 同一取法，但按期间保留，不去掉 0）。"""
    def cell(stamp: str) -> dict:
        return truth[ref["table"]].get(str((*ref["key"], stamp)) if ref.get("key") else stamp) or {}

    def value(start: str, end: str) -> Decimal:
        if ref.get("period") == "day":
            first, last = date.fromisoformat(start), date.fromisoformat(end)
            rows = [cell(str(first + timedelta(days=i))) for i in range((last - first).days + 1)]
        else:
            rows = [cell(start[:7])]
        num, _, den = ref["measure"].partition("/")
        top = sum((Decimal(str(r[num])) for r in rows if r), Decimal("0"))
        if not den:
            return top
        bottom = sum((Decimal(str(r[den])) for r in rows if r), Decimal("0"))
        return top / bottom if bottom else Decimal("0")
    current = value(window["start"], window["end"])
    compare = value(window["compare_start"], window["compare_end"])
    return {"current": current, "compare": compare, "change": current - compare}


def independent_change(truth: dict, spec: dict, window: dict) -> dict[str, Any]:
    """业务真值（不调用产品函数）：视图日明细按题目范围筛选、按窗口求两期的度量和（比率为分子和 / 分母和）与变化方向。"""
    metric = v1._metric_specs()[spec["metric"]]
    facts = truth["views_daily"][metric["view"]]
    columns = facts["columns"]
    filters = {metric["dims"][name]: str(value) for name, value in (spec.get("filters") or {}).items()}
    sums = {"current": [Decimal(0)] * len(metric["measures"]), "compare": [Decimal(0)] * len(metric["measures"])}
    rows = {"current": 0, "compare": 0}
    spans = {"current": (window["start"], window["end"]), "compare": (window["compare_start"], window["compare_end"])}
    for raw in facts["rows"]:
        r = dict(zip(columns, raw))
        if any(str(r[c]) != v for c, v in filters.items()):
            continue
        for period, (start, end) in spans.items():
            if start <= str(r[columns[0]])[:10] <= end:
                rows[period] += 1
                sums[period] = [s + Decimal(str(r[m])) for s, m in zip(sums[period], metric["measures"])]
    if not rows["current"] or not rows["compare"]:
        return {"current": None, "compare": None, "change": None, "direction": None, "rows": rows}
    level = {p: (v[0] / v[1] if len(v) == 2 and v[1] else v[0] if len(v) == 1 else None) for p, v in sums.items()}
    if level["current"] is None or level["compare"] is None:
        return {"current": None, "compare": None, "change": None, "direction": None, "rows": rows}
    change = level["current"] - level["compare"]
    return {"current": str(level["current"]), "compare": str(level["compare"]), "change": str(change),
            "direction": "up" if change > 0 else "down" if change < 0 else "flat", "rows": rows}


# --- 数值引用的绑定 ---------------------------------------------------------------------------

PATH = re.compile(r"^(?P<ds>D\d+)\.(?P<col>.+?)(?:\[(?P<sel>[^\]]*)\])?$")
DAY = r"\d{4}-\d{2}-\d{2}"
GOVERNED_TITLE = re.compile(rf"^(?P<names>.+?) (?P<s>{DAY})~(?P<e>{DAY})(?: 按(?P<group>\S+))?(?: 按(?P<grain>日|月))?"
                            rf"(?: 对比(?:(?P<cs>{DAY})~(?P<ce>{DAY})|(?P<prev>上一期|去年同期)))?$")
SUFFIX = {"·对比期": "compare", "·变化率": "rate", "·变化": "change"}


def _metric_names() -> dict[str, str]:
    from avelis.demo_setup import EC_DERIVED, EC_METRICS

    return {**{m[1]: m[0] for m in EC_METRICS}, **{m[1]: m[0] for m in EC_DERIVED}}


def _units() -> dict[str, str]:
    from avelis.demo_setup import EC_UNITS

    return dict(EC_UNITS)


def _shift(start: date, end: date, mode: str) -> tuple[date, date]:
    if mode == "previous_year":
        return start.replace(year=start.year - 1), end.replace(year=end.year - 1)
    days = (end - start).days + 1
    return start - timedelta(days=days), start - timedelta(days=1)


class RunIndex:
    """一次运行的数据集、产生它的步骤与运行窗口（GET /api/analysis/runs/{id} 的结果）。"""

    def __init__(self, run: dict):
        self.run = run
        self.datasets = {d["ref"]: d for d in run.get("datasets") or []}
        self.producer = {s["dataset"]: s for s in run.get("steps") or [] if s.get("dataset")}
        self.window = run.get("window") or {}
        self.names = _metric_names()

    def _window(self, args: dict) -> tuple[tuple[str, str] | None, tuple[str, str] | None]:
        start = args.get("start_date") or self.window.get("start")
        end = args.get("end_date") or self.window.get("end")
        current = (str(start), str(end)) if start and end else None
        compare = None
        if args.get("compare") == "custom":
            cs = args.get("compare_start") or self.window.get("compare_start")
            ce = args.get("compare_end") or self.window.get("compare_end")
            compare = (str(cs), str(ce)) if cs and ce else None
        elif args.get("compare") in ("previous_period", "previous_year") and current:
            s, e = _shift(date.fromisoformat(current[0]), date.fromisoformat(current[1]), args["compare"])
            compare = (str(s), str(e))
        return current, compare

    def context(self, ref: str, depth: int = 0) -> dict | None:
        """数据集的口径语境：指标名 → 编码、筛选、本期与对比期。受治理查询取自产生它的步骤；计算结果沿第一个输入追溯。"""
        ds = self.datasets.get(ref)
        if ds is None or depth > 6:
            return None
        step = self.producer.get(ref)
        args = (step or {}).get("args") or {}
        if ds["kind"] == "governed":
            # 受治理查询的标题是确定的格式（tools._run_query）：指标名 本期 [按维度] [按日 / 按月] [对比 对比期]；
            # 筛选取自产生它的 query_metric 步骤，explain_change 的子查询没有自己的步骤，取那次 explain_change 的筛选
            t = GOVERNED_TITLE.match(ds["title"])
            if t is None:
                return None
            if not (step and step.get("tool") == "query_metric"):
                step = self._parent_explain(ref)
                if step is None:
                    return None
                args = step.get("args") or {}
            names = t["names"].split("、")
            codes = args.get("metric_codes") or []
            mapping = dict(zip(names, codes)) if len(names) == len(codes) else {n: self.names.get(n) for n in names}
            grain = t["grain"] or (t["group"] if t["group"] in ("日", "月") else None)
            group = [] if t["group"] in (None, "日", "月") else t["group"].split("、")
            compare = (t["cs"], t["ce"]) if t["cs"] else None
            if t["prev"] and args.get("compare") in ("previous_period", "previous_year"):
                s, e = _shift(date.fromisoformat(t["s"]), date.fromisoformat(t["e"]), args["compare"])
                compare = (str(s), str(e))
            return {"names": mapping, "filters": args.get("filters") or [], "group_by": group,
                    "time_grain": grain,
                    "current": (t["s"], t["e"]), "compare": compare,
                    "metric": next(iter(mapping.values())) if len(mapping) == 1 else None}
        if step and step.get("tool") == "explain_change":
            ctx = {**(self.context((ds.get("inputs") or [None])[0], depth + 1) or {})}
            current, compare = self._window({**args, "compare": "custom"})
            ctx.update(metric=args.get("metric_code"), filters=args.get("filters") or [], group_by=[],
                       time_grain=None, current=current, compare=compare)
            return ctx
        inputs = ds.get("inputs") or []
        if step and step.get("tool") in ("contribution", "calendar_adjust", "factor_decompose") and args.get("dataset"):
            return self.context(args["dataset"], depth + 1)
        if not step and inputs:  # explain_change 内部产生的因子拆解：语境取总量查询
            return self.context(inputs[0], depth + 1)
        return None

    def _parent_explain(self, ref: str) -> dict | None:
        for s in self.run.get("steps") or []:
            if s.get("tool") == "explain_change" and s.get("dataset"):
                if ref in (self.datasets.get(s["dataset"]) or {}).get("inputs") or []:
                    return s
        return None

    def bind(self, segment: dict) -> tuple[dict | None, str | None]:
        """一个数值引用段 → 绑定 {source, metric, entity, period, calc, unit}；绑定不了时返回 (None, 原因)。"""
        m = PATH.match(segment.get("path") or "")
        if not m:
            return None, f"引用路径无法解析：{segment.get('path')}"
        ref, col, sel = m["ds"], m["col"], m["sel"]
        ds = self.datasets.get(ref)
        if ds is None:
            return None, f"{ref} 不在运行记录里"
        step = self.producer.get(ref)
        tool = (step or {}).get("tool")
        if ds["kind"] == "explore":
            return {"source": "explore", "sql": ds.get("sql") or "", "column": col}, None
        if tool == "derive" or ds["title"].startswith("派生："):
            return None, f"{ref} 是派生计算（derive），公式语义不能自动绑定"
        row, why = self._row(ds, sel)
        if why and not col.startswith("summary."):
            return None, why
        ctx = self.context(ref)
        if ctx is None:
            return None, f"{ref}（{tool or ds['kind']}）追溯不到受治理查询的口径语境"
        entity = {}
        for f in ctx.get("filters") or []:
            values = sorted({str(v) for v in f.get("values") or []})
            entity[f["dimension"]] = values[0] if len(values) == 1 else tuple(values)
        unit = segment.get("unit")
        if ds["kind"] == "governed":
            if ctx.get("time_grain"):
                return None, f"{ref} 按{ctx['time_grain']}分组：数值是单日 / 单月的值，不是题目期间"
            for g in ctx.get("group_by") or []:
                if row is None:
                    return None, f"{ref} 按 {g} 分组但没有选定行"
                entity[g] = str(row.get(g))
            base, period = col, "current"
            for suffix, p in SUFFIX.items():
                if col.endswith(suffix):
                    base, period = col[: -len(suffix)], p
                    break
            metric = ctx["names"].get(base) or self.names.get(base)
            calc = {"current": "level", "compare": "level", "change": "delta", "rate": "rate"}[period]
            return {"source": "governed", "metric": metric, "entity": entity, "calc": calc, "unit": unit,
                    "span": self._span(ctx, period), "period": period}, None
        if tool == "explain_change" or {"dimension", "member", "member_change"} <= set(ds["columns"]):
            if row is None:
                return None, f"{ref} 没有选定行"
            entity[str(row.get("dimension"))] = str(row.get("member"))
            period = {"previous": "compare", "current": "current", "member_change": "change"}.get(col)
            if col == "contribution":  # 可加指标的成员贡献就是成员变化量；比率指标的贡献不是
                period = "change" if row.get("structure_effect") in (None, "") else None
            if period is None:
                return None, f"{ref}.{col} 不是成员的两期值或变化量"
            return {"source": "explain", "metric": ctx.get("metric"), "entity": entity,
                    "calc": "level" if period in ("current", "compare") else "delta", "unit": unit,
                    "span": self._span(ctx, period), "period": period}, None
        if "factor_metric_code" in ds["columns"]:
            if row is None:
                return None, f"{ref} 没有选定行"
            if col not in ("previous", "current"):
                return None, f"{ref}.{col} 是因子贡献，不是因子本身的两期值"
            period = "compare" if col == "previous" else "current"
            return {"source": "factor", "metric": row.get("factor_metric_code"), "entity": entity, "calc": "level",
                    "unit": unit, "span": self._span(ctx, period), "period": period}, None
        if tool == "calendar_adjust":
            args = step.get("args") or {}
            entity.update({str(k): str(v) for k, v in (args.get("match") or {}).items()})  # 在分组结果里选定的行
            metric = (ctx.get("names") or {}).get(args.get("current")) or ctx.get("metric")
            if col.startswith("summary."):
                key = col.split(".", 1)[1]
                calc = "delta" if key == "change" else f"calendar.{key}"
                return {"source": "calendar", "metric": metric, "entity": entity, "calc": calc, "unit": unit,
                        "span": self._span(ctx, "change"), "period": "change"}, None
            return None, f"{ref}.{col} 是日历折算的明细列"
        if tool == "contribution":
            args = step.get("args") or {}
            if row is None:
                return None, f"{ref} 没有选定行"
            for k in args.get("keys") or []:
                entity[k] = str(row.get(k))
            period = {"previous": "compare", "current": "current", "change": "change"}.get(col)
            if period is None:
                return None, f"{ref}.{col} 不是两期值或变化量"
            base = args.get("current") or ""
            metric = (ctx.get("names") or {}).get(base) or self.names.get(base)
            return {"source": "contribution", "metric": metric, "entity": entity,
                    "calc": "level" if period != "change" else "delta", "unit": unit,
                    "span": self._span(ctx, period), "period": period}, None
        return None, f"{ref}（{tool or ds['kind']}）的列 {col} 没有可自动核对的语义"

    @staticmethod
    def _span(ctx: dict, period: str) -> tuple:
        if period == "current":
            return (ctx.get("current"),)
        if period == "compare":
            return (ctx.get("compare"),)
        return (ctx.get("current"), ctx.get("compare"))

    @staticmethod
    def _row(ds: dict, sel: str | None) -> tuple[dict | None, str | None]:
        rows = [dict(zip(ds["columns"], r)) for r in ds.get("rows") or []]
        if not sel:
            return (rows[0], None) if len(rows) == 1 else (None, None)
        if sel.startswith("#"):
            n = int(sel[1:])
            if n > len(rows):
                return None, f"{ds['ref']} 第 {n} 行不在运行记录保存的前 {len(rows)} 行里"
            return rows[n - 1], None
        match = dict(part.split("=", 1) for part in sel.split(",") if "=" in part)
        hits = [r for r in rows if all(str(r.get(k)) == v for k, v in match.items())]
        if len(hits) != 1:
            return None, (f"{ds['ref']} 按 {match} 在保存的前 {len(rows)} 行里选中 {len(hits)} 行"
                          + ("（数据集超过保存行数）" if ds.get("row_count", 0) > len(rows) else ""))
        return hits[0], None


def _value_segments(report: dict) -> list[dict]:
    records = [*(report.get("fields") or {}).values(),
               *(c for c in report.get("claims") or [] if c.get("type") in v1.FACT_TYPES),
               *((c.get("contribution") or {}) for c in report.get("causes") or [])]
    return [s for r in records for s in r.get("segments") or [] if s.get("kind") in ("value", "count")]


def _binding_ok(spec: dict, got: dict, q: dict) -> str | None:
    """绑定与题目定义是否一致；不一致时返回原因。"""
    window = q["window"]
    spans = {"current": ((window["start"], window["end"]),), "compare": ((window["compare_start"], window["compare_end"]),),
             "change": ((window["start"], window["end"]), (window["compare_start"], window["compare_end"]))}
    if "explore" in spec:
        table, column = spec["explore"]["table"], spec["explore"].get("column")
        if got.get("source") != "explore":
            return "不是来自所需表的探索数据集"
        if table not in re.sub(r"[`\"\[\]]", "", got.get("sql", "")):
            return f"探索 SQL 没有引用 {table}"
        if column and got.get("column") != column:
            return f"列是 {got.get('column')}，不是 {column}"
        return None
    if got.get("source") == "explore":
        return "探索数据集没有口径语义，不能作为这个指标的关键数值"
    if got.get("metric") != spec["metric"]:
        return f"指标是 {got.get('metric')}，不是 {spec['metric']}"
    entity = dict(got.get("entity") or {})
    for dim in spec.get("allow") or []:
        if dim not in spec["entity"]:
            entity.pop(dim, None)
    if entity != spec["entity"]:
        return f"实体 / 筛选是 {entity}，不是 {spec['entity']}"
    want_calc = spec.get("calc")
    if want_calc and got.get("calc") != want_calc:
        return f"计算类型是 {got.get('calc')}，不是 {want_calc}"
    if not want_calc and got.get("calc") not in ("level", "delta"):
        return f"计算类型是 {got.get('calc')}，不是水平值或变化量"
    if tuple(got.get("span") or ()) != spans[got.get("period") or "change"]:
        return f"期间是 {got.get('span')}，不是题目的{ {'current': '本期', 'compare': '对比期'}.get(got.get('period'), '两期')}"
    unit = _units().get(spec["metric"])
    if unit and got.get("unit") not in (None, "", unit):
        return f"单位是 {got.get('unit')}，不是 {unit}"
    return None


def key_value(q: dict, report: dict, run: dict) -> dict[str, Any]:
    """关键数值的绑定判分：命中、逐个候选引用的不命中原因（最多 8 条）。"""
    key = (q.get("expect") or {}).get("key_value")
    bindings = (q.get("v2") or {}).get("key_bindings") or []
    if not key:
        return {"hit": None, "reason": "题目没有关键数值"}
    if any(b.get("manual") for b in bindings):
        return {"hit": None, "reason": "关键数值需人工审定（key_bindings.manual）"}
    if not bindings:
        return {"hit": None, "reason": "缺少 v2 绑定定义"}
    tolerance = Decimal(str(key.get("tolerance", 0)))
    index, misses = RunIndex(run), []
    for seg in _value_segments(report):
        raw = v1._number(seg.get("raw"))
        if raw is None:
            continue
        got, why = index.bind(seg)
        for spec in bindings:
            candidates = spec["truth"] if spec.get("truth") else {"value": Decimal(str(spec["value"]))}
            matched = [p for p, e in candidates.items()
                       if v1._within(abs(raw) if key.get("abs") else raw, abs(e) if key.get("abs") else e, tolerance)]
            if not matched:
                continue
            if got is None:
                misses.append(f"{seg.get('path')}：数值相同但{why}")
                continue
            if spec.get("truth") and got.get("source") != "explore" and got.get("period") not in matched:
                misses.append(f"{seg.get('path')}：数值等于真值的{'、'.join(matched)}，但所在列是 {got.get('period')}")
                continue
            problem = _binding_ok(spec, got, q)
            if problem is None:
                return {"hit": True, "path": seg.get("path"), "binding": _plain(got)}
            misses.append(f"{seg.get('path')}：数值相同但{problem}")
    return {"hit": False, "reason": "；".join(dict.fromkeys(misses)) [:1200] or "报告里没有等于真值的数值引用"}


def _plain(b: dict) -> dict:
    return {k: (list(v) if isinstance(v, tuple) else v) for k, v in b.items() if k != "sql"}


# --- 权限与审计 --------------------------------------------------------------------------------

def audit(q: dict, run: dict) -> dict[str, Any]:
    """无权对象的审计核对：成功的步骤与交付的数据集里不得出现无权的口径编码或库表。"""
    spec = (q.get("v2") or {}).get("unauthorized")
    if not spec:
        return {"checked": False, "violations": []}
    prefix, db = spec.get("metric_prefix"), spec.get("table_db_prefix")
    table = re.compile(rf"(?:^|[\s,(.\"]){re.escape(db)}[a-z0-9_]*\.", re.I) if db else None
    violations = []
    for s in run.get("steps") or []:
        if s.get("status") != "ok" or s.get("kind") != "tool":
            continue
        args = s.get("args") or {}
        codes = [*(args.get("metric_codes") or []), args.get("metric_code"), args.get("code")]
        if prefix and any(isinstance(c, str) and c.startswith(prefix) for c in codes):
            violations.append(f"步骤 {s['seq']} {s['tool']} 成功使用了无权口径")
        if table and table.search(str(args.get("sql") or "") + " " + str(args.get("asset") or "")
                                  + " " + str(args.get("qualified") or "")):
            violations.append(f"步骤 {s['seq']} {s['tool']} 成功读取了无权库表")
    for d in run.get("datasets") or []:
        if table and table.search(str(d.get("sql") or "")):
            violations.append(f"数据集 {d['ref']} 的来源含无权库表")
    return {"checked": True, "violations": violations}


def structured_denial(run: dict) -> dict | None:
    """结构化的权限拒绝：report.permission 的 denials（PermissionDenied）或 lost（PermissionLost）。"""
    permission = ((run or {}).get("report") or {}).get("permission") or {}
    if permission.get("lost") or permission.get("denials"):
        return {"lost": permission.get("lost"), "denials": len(permission.get("denials") or [])}
    return None


def infrastructure_error(run: dict | None) -> bool:
    """非内容原因的失败：传输、超时、截断、JSON 解析等（不能算作任何分析结论，包括正确拒绝）。"""
    if not run:
        return True
    text = str(run.get("error") or "")
    if v1.transport_error(run) or any(m in text for m in NON_CONTENT_ERRORS):
        return True
    return any((s.get("output") or {}).get("error_code") in ("model_truncated", "model_protocol")
               for s in run.get("steps") or [])


# --- 判分 ------------------------------------------------------------------------------------

def score(question: dict, run: dict | None) -> dict[str, Any]:
    old = v1.score(question, run)
    expect = question.get("expect") or {}
    category = question["category"]
    report = (run or {}).get("report") or {}
    completed = old["completed"]
    kv = key_value(question, report, run or {}) if completed else {
        "hit": False if expect.get("key_value") else None, "reason": "运行没有完成"}
    audited = audit(question, run or {})
    business = question.get("business")
    premise_ok = None
    primary = expect.get("primary")
    if business and business.get("direction") and primary:
        premise_ok = business["direction"] == primary["direction"]
    out = {**{k: old[k] for k in ("id", "category", "status", "completed", "model_calls", "seconds", "model", "effort",
                                  "conclusion", "sources_failed", "forbidden", "primary_hit", "required_in_causes")},
           "scorer": VERSION,
           "policy_conclusion_ok": old["conclusion_ok"],
           "key_value_hit": kv["hit"], "key_value_detail": kv,
           "business": business, "premise_ok": premise_ok,
           "audit": audited, "denial": structured_denial(run), "infrastructure_error": infrastructure_error(run)
           if run and run.get("status") != "succeeded" else False}
    clean = old["sources_failed"] == 0 and not old["forbidden"] and not audited["violations"]
    core = bool(completed and clean and old["conclusion_ok"])
    if category in v1.POSITIVE or (category == "adversarial" and primary):
        core = bool(core and old["primary_hit"] and kv["hit"] is not False
                    and old["required_in_causes"] is not False and premise_ok is not False)
    out["policy_passed"] = bool(completed and clean and old["conclusion_ok"])
    out["passed"] = core
    if category == "adversarial" and expect.get("denied_ok") and not completed:
        # 正确拒绝：结构化权限拒绝 + 审计通过 + 不是基础设施失败
        out["passed"] = bool(out["denial"] and audited.get("checked") and not audited["violations"]
                             and not out["infrastructure_error"])
        out["policy_passed"] = out["passed"]
    return out


def compare(question: dict, run: dict | None) -> dict[str, Any]:
    """旧 / 新判分并列，并写出差异原因。"""
    old, new = v1.score(question, run), score(question, run)
    reasons = []
    if old["passed"] != new["passed"]:
        if question["category"] == "adversarial" and (question.get("expect") or {}).get("denied_ok") and not old["completed"]:
            reasons.append("对抗题未完成：v1 把失败 / 暂停算作权限拒绝，v2 要求结构化拒绝码与审计证据")
        if old["key_value_hit"] and new["key_value_hit"] is False:
            reasons.append("关键数值：v1 在报告任意位置命中，v2 绑定不成立（" + new["key_value_detail"]["reason"][:300] + "）")
        if new["premise_ok"] is False:
            reasons.append("业务真值的变化方向与题目预期方向不一致（题目前提需复核）")
        if new["audit"]["violations"]:
            reasons.append("审计：" + "；".join(new["audit"]["violations"][:3]))
        if not reasons:
            reasons.append("其他")
    elif old["key_value_hit"] is not None and old["key_value_hit"] != new["key_value_hit"]:
        reasons.append("关键数值命中改变但不影响通过：" + str(new["key_value_detail"].get("reason") or "")[:300])
    return {"id": question["id"], "category": question["category"], "v1_passed": old["passed"],
            "v2_passed": new["passed"], "v2_policy_passed": new["policy_passed"], "v1_key": old["key_value_hit"],
            "v2_key": new["key_value_hit"], "reasons": reasons, "v2": new}
