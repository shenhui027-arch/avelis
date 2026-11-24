"""Ask 模式评测判分器（M3D Part B）：纯函数，覆盖检索、对齐、多指标排序、负样本拒绝与解读数字核验。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

ASK_QUESTIONS = Path(__file__).with_name("ask_questions.yaml")


def load_ask_questions(path: Path = ASK_QUESTIONS) -> dict[str, Any]:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    defaults = data.get("defaults") or {}
    for q in data["questions"]:
        q.setdefault("user", defaults.get("user", "wang"))
        q.setdefault("window", dict(defaults.get("window") or {}))
    return data


def score_ask_case(question: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    category = question["category"]
    expect = question.get("expect") or {}
    checks: dict[str, bool] = {}

    if category == "retrieval":
        codes = list(outcome.get("ranked_codes") or [])
        top_expected = expect.get("top_codes") or []
        within = int(expect.get("rank_within") or 1)
        if top_expected:
            checks["top_hit"] = any(c in codes[:within] for c in top_expected)
        else:
            checks["top_hit"] = len(codes) == 0
        forbidden = set(expect.get("forbidden_codes") or [])
        checks["no_forbidden"] = not (set(codes) & forbidden)
    elif category == "alignment":
        checks["status_ok"] = outcome.get("status") == expect.get("status")
        aligned = outcome.get("aligned_filters") or {}
        checks["filters_ok"] = all(aligned.get(k) == v for k, v in (expect.get("aligned_filters") or {}).items())
        if expect.get("require_alignment_evidence"):
            checks["evidence_ok"] = bool(outcome.get("alignment_evidence"))
        if expect.get("clarify_options_include"):
            opts = set(outcome.get("clarify_options") or [])
            checks["clarify_ok"] = set(expect["clarify_options_include"]) <= opts
    elif category == "multi_order":
        checks["status_ok"] = outcome.get("status") == expect.get("status")
        if "metric_codes" in expect:
            checks["metrics_ok"] = outcome.get("metric_codes") == expect["metric_codes"]
        if "group_by" in expect:
            checks["group_by_ok"] = outcome.get("group_by") == expect["group_by"]
        if "order" in expect:
            checks["order_ok"] = outcome.get("order") == expect["order"]
        if "limit" in expect:
            checks["limit_ok"] = outcome.get("limit") == expect["limit"]
        if "compare" in expect:
            checks["compare_ok"] = outcome.get("compare") == expect["compare"]
        if "max_rows" in expect:
            checks["row_cap_ok"] = len(outcome.get("rows") or []) <= int(expect["max_rows"])
        if expect.get("sorted_desc") or expect.get("sorted_asc"):
            vals = [r[-1] for r in (outcome.get("rows") or [])]
            checks["sorted_ok"] = vals == sorted(vals, reverse=bool(expect.get("sorted_desc")))
    elif category == "negative":
        checks["status_ok"] = outcome.get("status") in (expect.get("status_in") or [])
        if expect.get("no_sql"):
            checks["no_sql"] = not outcome.get("sql")
        if expect.get("forbidden_text"):
            body = f"{outcome.get('message') or ''} {outcome.get('explanation') or ''}"
            checks["no_hallucination"] = all(tok not in body for tok in expect["forbidden_text"])
    elif category == "grounding":
        checks["source_ok"] = outcome.get("explanation_source") == expect.get("explanation_source")
        if "grounding_ok" in expect:
            checks["grounding_ok"] = outcome.get("grounding_ok") is expect["grounding_ok"]
        if "ungrounded_count" in expect:
            checks["ungrounded_count_ok"] = outcome.get("ungrounded_count") == expect["ungrounded_count"]
        if "fallback" in expect:
            checks["fallback_ok"] = outcome.get("fallback") == expect["fallback"]
        if "model_calls" in expect:
            checks["calls_ok"] = outcome.get("model_calls") == expect["model_calls"]
        if expect.get("forbidden_text"):
            body = str(outcome.get("explanation") or "")
            checks["no_leak"] = all(tok not in body for tok in expect["forbidden_text"])
    else:
        checks["known_category"] = False

    return {
        "id": question["id"],
        "category": category,
        "checks": checks,
        "passed": bool(checks) and all(checks.values()),
    }


def summarize_ask(results: list[dict[str, Any]]) -> dict[str, Any]:
    def rate(items: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(items)
        hits = sum(1 for r in items if r.get("passed"))
        return {"runs": n, "hits": hits, "rate": round(hits / n, 4) if n else None}

    by_category: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        by_category.setdefault(r["category"], []).append(r)
    return {
        "overall": rate(results),
        "by_category": {cat: rate(items) for cat, items in by_category.items()},
    }
