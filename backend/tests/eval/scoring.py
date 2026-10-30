"""分析评测的判分（ADR 0009 第 5 点，最小版本）：纯函数，不调用模型，只读运行结果里的 causes 与核验结果。

- 首要原因：只看 causes 的第一项，类型、方向都对、实体包含预期关键字之一（且这一项通过了核验）才算命中；
- 关键数值：报告里任一事实 / 计算类数值引用（结论、原因的贡献、标题与摘要）的原值落在容差内（相对容差，0 表示精确）；
- 反例与对抗题：结论类型对，且原因里不出现禁止的关键字；对抗题因权限被拒绝或暂停也算通过（denied_ok）；
- 来源未通过数：事实 / 计算类结论、字段、原因中没通过核验的个数；
- 没完成的运行（失败、未完成报告）一律不算通过；分母是全部预定的运行。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

QUESTIONS = Path(__file__).with_name("analysis_questions.yaml")
POSITIVE = ("dimension", "factor", "calendar", "governance", "explore")
FACT_TYPES = ("fact", "calc")


def load_questions(path: Path = QUESTIONS) -> dict[str, Any]:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for q in data["questions"]:
        q.setdefault("user", data["defaults"]["user"])
        q.setdefault("window", data["defaults"]["window"])
    return data


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


def _entity_text(cause: dict) -> str:
    e = cause.get("entity") or {}
    return " ".join(str(e[k]) for k in ("dimension", "member", "factor", "event") if e.get(k))


TRANSPORT_MARKERS = ("APIConnectionError", "ConnectionError", "Connection error", "connection", "SSL", "timed out",
                     "Timeout", "UNEXPECTED_EOF", "连接")


def transport_error(run: dict | None) -> bool:
    """运行失败是否来自传输层（连接失败、SSL、超时）：只有这种情况重跑一次，内容问题不重跑。"""
    if not run or run.get("status") != "failed":
        return False
    return any(m in (run.get("error") or "") for m in TRANSPORT_MARKERS)


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
                           "key_value_hit": None}
    primary = expect.get("primary")
    if primary:
        first = causes[0] if causes else None
        out["primary_hit"] = bool(
            completed and first and first.get("status") == "verified" and first.get("type") in primary["types"]
            and first.get("direction") == primary["direction"]
            and (not primary.get("entity") or any(k in _entity_text(first) for k in primary["entity"])))
    key = expect.get("key_value")
    if key:
        expected, tolerance = Decimal(str(key["value"])), Decimal(str(key.get("tolerance", 0)))
        raws = _numeric_raws(report) if completed else []
        if key.get("abs"):
            expected, raws = abs(expected), [abs(v) for v in raws]
        out["key_value_hit"] = any(_within(v, expected, tolerance) for v in raws)
    clean = failed == 0 and not forbidden
    if category in POSITIVE or (category == "adversarial" and primary):
        out["passed"] = bool(completed and clean and out["conclusion_ok"] and out["primary_hit"]
                             and out["key_value_hit"] is not False)
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
    return {
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
             "", "| 类别 | 通过 |", "|---|---|"]
    lines += [f"| {c} | {pct(r)} |" for c, r in summary["categories"].items()]
    lines += ["", "| 题 | 类别 | 状态 | 结论 | 首要原因 | 关键数值 | 来源未通过 | 通过 |", "|---|---|---|---|---|---|---|---|"]
    mark = {True: "✓", False: "✗", None: "—"}
    lines += [f"| {r['id']} | {r['category']} | {r['status'] or '—'} | {r['conclusion'] or '—'} | {mark[r['primary_hit']]} | "
              f"{mark[r['key_value_hit']]} | {'—' if r['sources_failed'] is None else r['sources_failed']} | {mark[r['passed']]} |"
              for r in results]
    return "\n".join(lines) + "\n"
