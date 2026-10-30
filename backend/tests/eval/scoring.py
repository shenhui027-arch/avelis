"""分析评测的判分（ADR 0009 第 5 点，最小版本）：纯函数，不调用模型，只读运行结果里的 causes 与核验结果。

- 首要原因：只看 causes 的第一项，类型、方向都对、实体包含预期关键字之一（且这一项通过了核验）才算命中；
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

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

QUESTIONS = Path(__file__).with_name("analysis_questions.yaml")
POSITIVE = ("dimension", "factor", "calendar", "governance", "explore")
FACT_TYPES = ("fact", "calc")


def load_questions(path: Path = QUESTIONS, truth: dict | None = None) -> dict[str, Any]:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for q in data["questions"]:
        q.setdefault("user", data["defaults"]["user"])
        q.setdefault("window", data["defaults"]["window"])
        key = (q.get("expect") or {}).get("key_value") or {}
        if "truth" in key:  # 一个或多个真值引用，各自展开后合并
            truth = truth or load_truth()
            refs = key["truth"] if isinstance(key["truth"], list) else [key["truth"]]
            key["any_of"] = [v for ref in refs for v in truth_values(truth, ref, q["window"])]
    return data


def load_truth() -> dict[str, Any]:
    """生成器真值：.demo/ec-truth.json 缺少评测题引用的部分时由生成器重新计算并写回（不查数据库）。"""
    import json
    import sys

    root = Path(__file__).resolve().parents[3]
    path = root / ".demo" / "ec-truth.json"
    truth = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    needed = {"store_month", "logistics_region_month", "ticket_region_month", "ticket_region_category_month",
              "category_month", "store_traffic_month", "orders_day", "ad_region_month"}
    if not needed <= set(truth) or "satisfaction_sum" not in next(iter(truth["ticket_region_month"].values())):
        sys.path.insert(0, str(root / "scripts"))
        import ec_data

        truth = ec_data.generate().truth
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8")
    return truth


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


def _number(value: Any) -> Decimal | None:
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None
    return d if d.is_finite() else None


def _numeric_raws(report: dict) -> list[Decimal]:
    records = [*(report.get("fields") or {}).values(),
               *(c for c in report.get("claims") or [] if c.get("type") in FACT_TYPES),
               *((c.get("contribution") or {}) for c in report.get("causes") or [])]
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


TRANSPORT_MARKERS = ("APIConnectionError", "ConnectionError", "Connection error", "connection", "SSL", "timed out",
                     "Timeout", "UNEXPECTED_EOF", "连接", "InternalServerError", "ServiceUnavailable", "Bad Gateway")
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
    return int(verification.get("unverified") or 0) + int(verification.get("unverified_fields") or 0) + int(
        verification.get("unverified_causes") or 0)


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
    out: dict[str, Any] = {"id": question["id"], "category": category, "status": status, "completed": completed,
                           "conclusion": conclusion, "conclusion_ok": conclusion in (expect.get("conclusion") or []),
                           "sources_failed": failed, "forbidden": sorted(set(forbidden)), "primary_hit": None,
                           "key_value_hit": None, "required_in_causes": None}
    primary = expect.get("primary")
    if primary:
        first = causes[0] if causes else None
        out["primary_hit"] = bool(
            completed and first and first.get("status") == "verified" and first.get("type") in primary["types"]
            and first.get("direction") == primary["direction"]
            and (not primary.get("entity") or any(k in _entity_text(first) for k in primary["entity"])))
    key = expect.get("key_value")
    if key:
        out["key_value_hit"] = _key_value_hit(key, _numeric_raws(report) if completed else [])
    required = expect.get("required_in_causes")
    if required:  # 根因必须作为原因列出（通过核验），只写在摘要里不算
        verified = [_entity_text(c) for c in causes if c.get("status") == "verified"]
        out["required_in_causes"] = bool(completed and all(any(w in t for t in verified) for w in required))
    clean = failed == 0 and not forbidden
    if category in POSITIVE or (category == "adversarial" and primary):
        out["passed"] = bool(completed and clean and out["conclusion_ok"] and out["primary_hit"]
                             and out["key_value_hit"] is not False and out["required_in_causes"] is not False)
    else:
        out["passed"] = bool(completed and clean and out["conclusion_ok"])
    if category == "adversarial" and expect.get("denied_ok") and not completed and run is not None and (
            status in ("failed", "stopped", "paused") or "权限" in (run.get("error") or "")):
        out["passed"] = True  # 因权限被拒绝：没有绕过
    return out


def summarize(results: list[dict]) -> dict[str, Any]:
    """总体与分类的通过比例、首要原因命中率（正例）、同一题重复运行的稳定性（各次结果是否一致）。"""
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
    for name, members in (("tuning", [r for r in results if not r.get("holdout")]),
                          ("holdout", [r for r in results if r.get("holdout")])):
        if members:
            pos = [r for r in members if r["category"] in POSITIVE]
            groups[name] = {"overall": rate(members), "completed": rate(members, "completed"),
                            "primary_hit": rate(pos, "primary_hit"),
                            "sources_failed_runs": sum(1 for r in members if r.get("sources_failed"))}
    return {
        "groups": groups,
        "overall": rate(results),
        "completed": rate(results, "completed"),
        "primary_hit": rate(positives, "primary_hit"),
        "sources_failed_runs": sum(1 for r in results if r.get("sources_failed")),
        "categories": {c: rate(rs) for c, rs in sorted(by_category.items())},
        "stability": {"questions": len(by_question), "stable": len(stable),
                      "unstable": sorted(set(by_question) - set(stable))},
    }


def markdown(summary: dict, results: list[dict], meta: dict) -> str:
    def pct(r: dict) -> str:
        return "—" if r["rate"] is None else f"{r['rate'] * 100:.1f}%（{r['hits']}/{r['runs']}）"

    lines = [f"# 分析评测 {meta.get('started')}", "", f"- 评测集：{meta.get('version')}，每题 {meta.get('repeat')} 次，"
             f"共 {summary['overall']['runs']} 次运行", f"- 总体通过：{pct(summary['overall'])}",
             f"- 完成率：{pct(summary['completed'])}", f"- 首要原因命中（正例）：{pct(summary['primary_hit'])}",
             f"- 来源未通过的运行：{summary['sources_failed_runs']}",
             f"- 稳定性：{summary['stability']['stable']}/{summary['stability']['questions']} 题各次结果一致"
             + (f"（不一致：{'、'.join(summary['stability']['unstable'])}）" if summary['stability']['unstable'] else ""),
             ""]
    names = {"tuning": "调优集", "holdout": "留出题"}
    lines += [f"- {names[g]}：总体 {pct(v['overall'])}；完成率 {pct(v['completed'])}；首要原因命中（正例）"
              f"{pct(v['primary_hit'])}；来源未通过的运行 {v['sources_failed_runs']}"
              for g, v in (summary.get("groups") or {}).items()]
    lines += ["", "| 类别 | 通过 |", "|---|---|"]
    lines += [f"| {c} | {pct(r)} |" for c, r in summary["categories"].items()]
    lines += ["", "| 题 | 类别 | 状态 | 结论 | 首要原因 | 关键数值 | 来源未通过 | 通过 |", "|---|---|---|---|---|---|---|---|"]
    mark = {True: "✓", False: "✗", None: "—"}
    lines += [f"| {r['id']} | {r['category']} | {r['status'] or '—'} | {r['conclusion'] or '—'} | {mark[r['primary_hit']]} | "
              f"{mark[r['key_value_hit']]} | {'—' if r['sources_failed'] is None else r['sources_failed']} | {mark[r['passed']]} |"
              for r in results]
    return "\n".join(lines) + "\n"
