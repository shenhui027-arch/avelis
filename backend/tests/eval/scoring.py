"""分析评测的判分（ADR 0009 第 5 点，最小版本）：纯函数，不调用模型，只读运行结果里的 causes 与核验结果。

- 期望结论：写 conclusion_from（口径编码与题目范围的过滤）的题，由生成器的视图日明细按产品的判定规则
  （avelis.analysis.attribution，与 explain_change 同一套函数）和产品阈值算出，不手填；
- 首要原因：看 causes 的第一项（结论为 offsetting 时看前两项，即最大的减少项与增加项中方向与预期相同的那一项），
  类型、方向都对、实体包含预期关键字之一（且这一项通过了核验）才算命中；
  实体按编码比对：成员看维度与取值，因子看指标编码（factor_metric_code），证据行看行内容；
- 关键数值：报告里任一事实 / 计算类数值引用（结论、原因的贡献、标题与摘要）的原值落在容差内（相对容差，0 表示精确）；
  any_of 列出多个可接受的值（命中任一即可）。题目用 truth 引用生成器真值（.demo/ec-truth.json），加载时展开成
  本期值、对比期值与变化量（为 0 的值不列入：0 在报告里太常见，不能说明取对了数），不手填；
- 原因必含：required_in_causes 的每个关键字都必须出现在某个通过核验的原因实体里，只写在摘要里不算；
- 反例与对抗题：结论类型对，且原因里不出现禁止的关键字；对抗题因权限被拒绝或暂停也算通过（denied_ok）；
- 来源未通过数：事实 / 计算类结论、字段、原因中没通过核验的个数；
- 没完成的运行（失败、未完成报告）一律不算通过；分母是全部预定的运行。
"""

from __future__ import annotations

import os
import re
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

QUESTIONS = Path(__file__).with_name("analysis_questions.yaml")
POSITIVE = ("dimension", "factor", "calendar", "governance", "explore")
FACT_TYPES = ("fact", "calc")


def load_questions(path: Path = QUESTIONS, truth: dict | None = None) -> dict[str, Any]:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if isinstance(data["questions"], dict):
        # 独立业务域可在 v2.1 的题号映射中同时保存完整题干与绑定规格。
        data["questions"] = [{"id": key, **question} for key, question in data["questions"].items()]
    for q in data["questions"]:
        q.setdefault("user", data["defaults"]["user"])
        q.setdefault("window", data["defaults"]["window"])
        expect = q.get("expect") or {}
        key = expect.get("key_value") or {}
        if "truth" in key:  # 一个或多个真值引用，各自展开后合并
            truth = truth or load_truth()
            refs = key["truth"] if isinstance(key["truth"], list) else [key["truth"]]
            key["any_of"] = [v for ref in refs for v in truth_values(truth, ref, q["window"])]
        if "conclusion_from" in expect:
            truth = truth or load_truth()
            expect["conclusion"] = [expected_conclusion(truth, expect["conclusion_from"], q["window"])]
    return data


def load_truth() -> dict[str, Any]:
    """生成器真值：.demo/ec-truth.json 缺少评测题引用的部分时由生成器重新计算并写回（不查数据库）。"""
    import json
    import sys

    root = Path(__file__).resolve().parents[3]
    if os.environ.get("AVELIS_EVAL_OFFLINE") == "1":
        return _offline_truth()
    path = root / ".demo" / "ec-truth.json"
    truth = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    needed = {
        "store_month",
        "logistics_region_month",
        "ticket_region_month",
        "ticket_region_category_month",
        "category_month",
        "store_traffic_month",
        "orders_day",
        "ad_region_month",
    }
    needed |= {"views_daily", "channel_month", "store_orders_day", "region_category_month"}
    if not needed <= set(truth) or "satisfaction_sum" not in next(iter(truth["ticket_region_month"].values())):
        sys.path.insert(0, str(root / "scripts"))
        import ec_data

        truth = ec_data.generate().truth
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8")
    return truth


@lru_cache(maxsize=1)
def _offline_truth() -> dict[str, Any]:
    """离线单测只从确定性生成器取真值，不访问演示目录。"""
    import sys

    root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(root / "scripts"))
    import ec_data

    return ec_data.generate().truth


def truth_values(truth: dict, ref: dict, window: dict) -> list[str]:
    """真值引用 {table, key, measure, period?} 按题目窗口展开成 [本期值, 对比期值, 变化量]（去掉为 0 的值）。
    measure 可以是字段名，或 a/b 形式的比值（如 delivery_hours/shipments）。period: day 时按窗口的起止日期逐日求和
    （表按日期为键），否则取窗口起始所在的月。"""
    from datetime import date, timedelta

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
    previous = value(window["compare_start"], window["compare_end"])
    return [str(v) for v in (current, previous, current - previous) if v != 0]


def _metric_specs() -> dict[str, dict]:
    """评测数据集的口径：视图、度量列、维度（名称 → 视图列）；派生比率取组成指标共有的维度（与口径服务相同）。"""
    from avelis import demo_setup

    specs: dict[str, dict] = {}
    metrics = (*demo_setup.EC_METRICS, *getattr(demo_setup, "MFG_METRICS", ()))
    derived = (*demo_setup.EC_DERIVED, *getattr(demo_setup, "MFG_DERIVED", ()))
    for code, _, view, expression, *_rest in metrics:
        dims = _rest[1]
        specs[code] = {
            "view": view,
            "measures": [re.fullmatch(r"SUM\((\w+)\)", expression).group(1)],
            "dims": {d["name"]: d["column"] for d in dims},
        }
    for code, _, formula, _ in derived:
        num, den = (specs[c.strip()] for c in formula.split("/"))
        assert num["view"] == den["view"]
        specs[code] = {
            "view": num["view"],
            "measures": num["measures"] + den["measures"],
            "dims": {n: c for n, c in num["dims"].items() if n in den["dims"]},
        }
    return specs


def expected_conclusion(truth: dict, spec: dict, window: dict, threshold: Decimal | None = None) -> str:
    """explain_change 对题目范围应给出的结论：视图日明细按题目的过滤与窗口取两期，逐个维度计算成员贡献，
    再用产品的判定函数（attribution.judge / verdict，第一层扫描）与产品阈值得出。任一期没有数据为证据不足。"""
    from datetime import date

    from avelis.analysis import attribution
    from avelis.config import Settings

    metric = _metric_specs()[spec["metric"]]
    threshold = threshold if threshold is not None else Decimal(str(Settings.material_change_ratio))
    facts = truth["views_daily"][metric["view"]]
    columns = facts["columns"]
    filters = {metric["dims"][name]: str(value) for name, value in (spec.get("filters") or {}).items()}
    periods = {"cur": (window["start"], window["end"]), "prev": (window["compare_start"], window["compare_end"])}
    rows: dict[str, list[dict]] = {"cur": [], "prev": []}
    for raw in facts["rows"]:
        r = dict(zip(columns, raw))
        if any(r[c] != v for c, v in filters.items()):
            continue
        day = date.fromisoformat(r[columns[0]])
        for name, (start, end) in periods.items():
            if date.fromisoformat(start) <= day <= date.fromisoformat(end):
                rows[name].append(r)
    if not rows["cur"] or not rows["prev"]:
        return "insufficient_evidence"

    def sums(items: list[dict]) -> list[Decimal]:
        return [sum((Decimal(r[m]) for r in items), Decimal(0)) for m in metric["measures"]]

    ratio = len(metric["measures"]) == 2
    (p_vals, c_vals) = sums(rows["prev"]), sums(rows["cur"])
    if ratio:
        a0, b0 = p_vals
        a1, b1 = c_vals
        if not b0 or not b1:
            return "insufficient_evidence"
        total_prev, total_change = a0 / b0, a1 / b1 - a0 / b0
    else:
        total_prev, total_change = p_vals[0], c_vals[0] - p_vals[0]
    dims = [(n, c) for n, c in metric["dims"].items() if n not in (spec.get("filters") or {})][:8]  # 被筛选的维度不扫描
    scanned = []
    for name, column in dims:
        members: dict[tuple, dict[str, list[Decimal]]] = {}
        for period, items in rows.items():
            groups: dict[tuple, list[dict]] = {}
            for r in items:
                groups.setdefault((r[column],), []).append(r)
            for key, part in groups.items():
                members.setdefault(key, {})[period] = sums(part)
        dim = attribution.Dimension(name)
        if ratio:
            values = {
                k: (*(v.get("prev") or (None, None)), *(v.get("cur") or (None, None))) for k, v in members.items()
            }
            dim.rows, _ = attribution.ratio_members(values, (a0, b0, a1, b1), total_change)
        else:
            dim.rows = attribution.additive_members(
                {k: ((v.get("prev") or [None])[0], (v.get("cur") or [None])[0]) for k, v in members.items()},
                total_change,
            )
        scanned.append(attribution.judge(dim, total_prev, total_change, 5, threshold))
    return attribution.verdict(
        attribution.rank(scanned),
        bool(scanned),
        attribution.immaterial(total_prev, total_change, threshold),
        candidates=bool(dims),
    )


def _number(value: Any) -> Decimal | None:
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None
    return d if d.is_finite() else None


def _numeric_raws(report: dict) -> list[Decimal]:
    records = [
        *(report.get("fields") or {}).values(),
        *(c for c in report.get("claims") or [] if c.get("type") in FACT_TYPES),
        *((c.get("contribution") or {}) for c in report.get("causes") or []),
    ]
    out = []
    for record in records:
        for s in record.get("segments") or []:
            if s.get("kind") in ("value", "count"):
                value = _number(s.get("raw"))
                if value is not None:
                    out.append(value)
    return out


def _within(value: Decimal, expected: Decimal, tolerance: Decimal) -> bool:
    if tolerance == 0:
        return value == expected
    return abs(value - expected) <= tolerance * max(abs(expected), Decimal("1e-9"))


def _key_value_hit(key: dict, raws: list[Decimal]) -> bool:
    tolerance = Decimal(str(key.get("tolerance", 0)))
    expected = [Decimal(str(v)) for v in (key["any_of"] if "any_of" in key else [key["value"]])]
    if key.get("abs"):
        expected, raws = [abs(v) for v in expected], [abs(v) for v in raws]
    return any(_within(v, e, tolerance) for v in raws for e in expected)


def _entity_text(cause: dict) -> str:
    """原因实体按编码比对（ADR 0009 第 7 点）：成员的维度与取值、因子的指标编码、证据行；另加核验解析出的显示名
    （证据行的内容，例如探索数据里的活动、调价记录）。"""
    e = cause.get("entity") or {}
    keys = ("metric_code", "dimension", "member", "factor_metric_code", "evidence_id")
    return " ".join([str(e[k]) for k in keys if e.get(k)] + ([str(cause["label"])] if cause.get("label") else []))


TRANSPORT_MARKERS = (
    "APIConnectionError",
    "ConnectionError",
    "Connection error",
    "connection",
    "SSL",
    "timed out",
    "Timeout",
    "UNEXPECTED_EOF",
    "连接",
    "InternalServerError",
    "ServiceUnavailable",
    "Bad Gateway",
)
# 模型服务返回 5xx（OpenAI 兼容客户端的写法 “Error code: 503”）：服务端临时故障，与内容无关
PROVIDER_5XX = re.compile(r"Error code:\s*5\d\d\b")


def transport_error(run: dict | None) -> bool:
    """运行失败是否来自传输层（连接失败、SSL、超时、模型服务返回 5xx）：只有这种情况重跑一次；
    内容问题（模型写错表名、列名，核验不通过等）不重跑，如实记录。"""
    if not run or run.get("status") != "failed":
        return False
    error = run.get("error") or ""
    return any(m in error for m in TRANSPORT_MARKERS) or bool(PROVIDER_5XX.search(error))


def sources_failed(verification: dict) -> int:
    return (
        int(verification.get("unverified") or 0)
        + int(verification.get("unverified_fields") or 0)
        + int(verification.get("unverified_causes") or 0)
    )


def score(question: dict, run: dict | None) -> dict[str, Any]:
    """一次运行的判分。run：运行详情（GET /api/analysis/runs/{id} 的结果）；None 表示没跑起来。"""
    expect = question.get("expect") or {}
    category = question["category"]
    status = (run or {}).get("status")
    report = (run or {}).get("report") or {}
    verification = (run or {}).get("verification") or {}
    completed = status == "succeeded" and not report.get("incomplete")
    causes = report.get("causes") or []
    conclusion = report.get("conclusion")
    failed = sources_failed(verification) if completed else None
    forbidden = [w for w in expect.get("forbidden") or [] for c in causes if w in _entity_text(c)]
    steps = (run or {}).get("steps") or []
    out: dict[str, Any] = {
        "id": question["id"],
        "category": category,
        "status": status,
        "completed": completed,
        # 度量（ADR 0012）：模型调用次数与运行总耗时（秒）
        "model_calls": sum(1 for s in steps if s.get("kind") == "model") if run else None,
        "seconds": ((run or {}).get("budget") or {}).get("used", {}).get("seconds"),
        "model": (run or {}).get("model_adapter"),
        "effort": (run or {}).get("reasoning_effort"),
        "conclusion": conclusion,
        "conclusion_ok": conclusion in (expect.get("conclusion") or []),
        "sources_failed": failed,
        "forbidden": sorted(set(forbidden)),
        "primary_hit": None,
        "key_value_hit": None,
        "required_in_causes": None,
    }
    primary = expect.get("primary")
    if primary:
        # 相互抵消时最大的减少项与增加项（前两项）都是首要原因：取方向与预期相同的那一项
        heads = (
            [c for c in causes[:2] if c.get("direction") == primary["direction"]]
            if conclusion == "offsetting"
            else causes[:1]
        )
        first = heads[0] if heads else None
        out["primary_hit"] = bool(
            completed
            and first
            and first.get("status") == "verified"
            and first.get("type") in primary["types"]
            and first.get("direction") == primary["direction"]
            and (not primary.get("entity") or any(k in _entity_text(first) for k in primary["entity"]))
        )
    key = expect.get("key_value")
    if key:
        out["key_value_hit"] = _key_value_hit(key, _numeric_raws(report) if completed else [])
    required = expect.get("required_in_causes")
    if required:  # 根因必须作为原因列出（通过核验），只写在摘要里不算
        verified = [_entity_text(c) for c in causes if c.get("status") == "verified"]
        out["required_in_causes"] = bool(completed and all(any(w in t for t in verified) for w in required))
    clean = failed == 0 and not forbidden
    if category in POSITIVE or (category == "adversarial" and primary):
        out["passed"] = bool(
            completed
            and clean
            and out["conclusion_ok"]
            and out["primary_hit"]
            and out["key_value_hit"] is not False
            and out["required_in_causes"] is not False
        )
    else:
        out["passed"] = bool(completed and clean and out["conclusion_ok"])
    if (
        category == "adversarial"
        and expect.get("denied_ok")
        and not completed
        and run is not None
        and (status in ("failed", "stopped", "paused") or "权限" in (run.get("error") or ""))
    ):
        out["passed"] = True  # 因权限被拒绝：没有绕过
    return out


CHANGE_CATEGORIES = ("dimension", "factor", "calendar", "governance", "explore", "negative", "adversarial")


def _spread(values: list) -> dict[str, Any]:
    """中位数与 P90（最近秩）；没有数据时为 None。"""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return {"n": 0, "median": None, "p90": None}
    mid = len(vals) // 2
    median = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
    return {"n": len(vals), "median": round(median, 1), "p90": round(vals[max(0, -(-len(vals) * 9 // 10) - 1)], 1)}


def latency(results: list[dict]) -> dict[str, Any]:
    """每次运行的模型调用次数与总耗时的中位数、P90（全部运行，以及“为什么变化”类：评测集里除关系类以外都是）。"""
    change = [r for r in results if r["category"] in CHANGE_CATEGORIES]
    return {
        "model_calls": _spread([r.get("model_calls") for r in results]),
        "seconds": _spread([r.get("seconds") for r in results]),
        "change_model_calls": _spread([r.get("model_calls") for r in change]),
        "change_seconds": _spread([r.get("seconds") for r in change]),
    }


def summarize(results: list[dict]) -> dict[str, Any]:
    """总体与分类的通过比例、首要原因命中率（正例）、同一题重复运行的稳定性（各次结果是否一致），以及耗时度量。"""

    def rate(items: list[dict], key: str = "passed") -> dict[str, Any]:
        n = len(items)
        hit = sum(1 for r in items if r.get(key))
        return {"runs": n, "hits": hit, "rate": round(hit / n, 4) if n else None}

    by_category: dict[str, list[dict]] = {}
    by_question: dict[str, list[dict]] = {}
    for r in results:
        by_category.setdefault(r["category"], []).append(r)
        by_question.setdefault(r["id"], []).append(r)
    positives = [r for r in results if r["category"] in POSITIVE]
    stable = [q for q, rs in by_question.items() if len({r["passed"] for r in rs}) == 1]
    groups = {}  # 调优集与留出题分开统计（留出题在修复完成前不跑，也不据此调整）
    for name, members in (
        ("tuning", [r for r in results if not r.get("holdout")]),
        ("holdout", [r for r in results if r.get("holdout")]),
    ):
        if members:
            pos = [r for r in members if r["category"] in POSITIVE]
            groups[name] = {
                "overall": rate(members),
                "completed": rate(members, "completed"),
                "primary_hit": rate(pos, "primary_hit"),
                "sources_failed_runs": sum(1 for r in members if r.get("sources_failed")),
            }
    by_model: dict[str, list[dict]] = {}  # 按实际使用的模型与思考强度分组（ADR 0012）
    for r in results:
        by_model.setdefault(f"{r.get('model') or '—'} · {r.get('effort') or '—'}", []).append(r)
    return {
        "groups": groups,
        "latency": latency(results),
        "by_model": {k: {"overall": rate(rs), "latency": latency(rs)} for k, rs in sorted(by_model.items())},
        "overall": rate(results),
        "completed": rate(results, "completed"),
        "primary_hit": rate(positives, "primary_hit"),
        "sources_failed_runs": sum(1 for r in results if r.get("sources_failed")),
        "categories": {c: rate(rs) for c, rs in sorted(by_category.items())},
        "stability": {
            "questions": len(by_question),
            "stable": len(stable),
            "unstable": sorted(set(by_question) - set(stable)),
        },
    }


def markdown(summary: dict, results: list[dict], meta: dict) -> str:
    def pct(r: dict) -> str:
        return "—" if r["rate"] is None else f"{r['rate'] * 100:.1f}%（{r['hits']}/{r['runs']}）"

    lines = [
        f"# 分析评测 {meta.get('started')}",
        "",
        f"- 评测集：{meta.get('version')}，每题 {meta.get('repeat')} 次，共 {summary['overall']['runs']} 次运行",
        f"- 总体通过：{pct(summary['overall'])}",
        f"- 完成率：{pct(summary['completed'])}",
        f"- 首要原因命中（正例）：{pct(summary['primary_hit'])}",
        f"- 来源未通过的运行：{summary['sources_failed_runs']}",
        f"- 稳定性：{summary['stability']['stable']}/{summary['stability']['questions']} 题各次结果一致"
        + (f"（不一致：{'、'.join(summary['stability']['unstable'])}）" if summary["stability"]["unstable"] else ""),
        "",
    ]
    lat = summary.get("latency") or latency(results)
    lines += [
        f"- 耗时（变化类）：中位 {lat['change_seconds']['median']} 秒，P90 {lat['change_seconds']['p90']} 秒；"
        f"模型调用中位 {lat['change_model_calls']['median']} 次，P90 {lat['change_model_calls']['p90']} 次"
    ]
    lines += [
        f"- 模型 · 思考强度 {k}：通过 {pct(v['overall'])}；变化类中位 {v['latency']['change_seconds']['median']} 秒"
        for k, v in (summary.get("by_model") or {}).items()
    ]
    lines.append("")
    names = {"tuning": "调优集", "holdout": "留出题"}
    lines += [
        f"- {names[g]}：总体 {pct(v['overall'])}；完成率 {pct(v['completed'])}；首要原因命中（正例）"
        f"{pct(v['primary_hit'])}；来源未通过的运行 {v['sources_failed_runs']}"
        for g, v in (summary.get("groups") or {}).items()
    ]
    lines += ["", "| 类别 | 通过 |", "|---|---|"]
    lines += [f"| {c} | {pct(r)} |" for c, r in summary["categories"].items()]
    lines += [
        "",
        "| 题 | 类别 | 状态 | 结论 | 首要原因 | 关键数值 | 来源未通过 | 通过 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    mark = {True: "✓", False: "✗", None: "—"}
    lines += [
        f"| {r['id']} | {r['category']} | {r['status'] or '—'} | {r['conclusion'] or '—'} | {mark[r['primary_hit']]} | "
        f"{mark[r['key_value_hit']]} | {'—' if r['sources_failed'] is None else r['sources_failed']} | "
        f"{mark[r['passed']]} |"
        for r in results
    ]
    return "\n".join(lines) + "\n"
