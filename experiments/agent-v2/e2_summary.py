"""E2 结果汇总（诊断用，不调用模型、不连数据库）：每个条件 × 场景的 v2.1.1 判分、费用、耗时，以及判断第一处偏离要用的轨迹特征。

  uv run --project backend python experiments/agent-v2/e2_summary.py <E2 结果 json>

第一处偏离由轨迹特征按固定规则初判（标为推断 B），不是人工审定；规则：
- 基础设施：运行失败且是传输 / 截断 / 协议错误；
- 范围：待澄清，或者理解出的范围与题目范围（指标、筛选）不一致；
- 证据未发现：首要原因没命中机制，模型和预检都没有读到口径上游闭包以外的表；
- 证据解释：首要原因没命中机制，但读到过闭包以外的表；
- 报告绑定：首要原因命中，但关键数值没绑定或来源未通过核验；
- 结论：政策结论不一致；
- 反例 / 对抗题按 v2.1.1 的通过与否单列。
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend" / "src")]

def tables_of(sql: str, governed: bool = False) -> set[str]:
    """库.表（小写）：用评分器 v2.1 的 sqlglot 解析（starrocks 方言）；解析失败的段跳过。"""
    from tests.eval import scoring_v2_1 as v21

    try:
        return set().union(*(v21.table_names(t) for t in v21.sql_trees(sql or "", governed)))
    except Exception:  # noqa: BLE001 - 诊断特征，解析不了就当没有
        return set()


def closure_of(run: dict) -> set[str]:
    """口径上游闭包：受治理数据集 SQL 里出现的视图 + 生成器视图定义的上游（只用作诊断特征）。"""
    from tests.eval import scoring_v2_1 as v21

    lineage = v21.view_lineage()
    out = set()
    for d in run.get("datasets") or []:
        if d.get("kind") == "governed":
            for t in tables_of(d.get("sql") or "", True):
                out |= {t, *lineage.get(t, set())}
    return out


def features(item: dict) -> dict:
    run = item.get("run") or {}
    score = item["score"]
    steps = run.get("steps") or []
    closure = closure_of(run)
    outside = set()
    for d in run.get("datasets") or []:
        if d.get("kind") == "explore":
            outside |= tables_of(d.get("sql") or "") - closure
    refs = []
    for s in steps:
        if s.get("tool") == "reference_tables" and s.get("status") == "ok":
            refs = [t["table"].split(".", 1)[1].lower() for t in (s.get("output") or {}).get("tables") or []]
    report = run.get("report") or {}
    causes = report.get("causes") or []
    first = causes[0] if causes else {}
    budget = (run.get("budget") or {}).get("used") or {}
    tools = collections.Counter(s.get("tool") for s in steps if s.get("kind") == "tool")
    return {
        "scenario": item["scenario"], "condition": item["condition"], "repeat": item["repeat"],
        "status": run.get("status"), "passed": score["passed"], "policy": score.get("policy_conclusion_ok"),
        "primary": score.get("primary_hit"), "key": score.get("key_value_hit"), "sources_failed": score.get("sources_failed"),
        "category": score.get("category"), "infra": score.get("infrastructure_error"),
        "revised": (run.get("verification") or {}).get("revised"),
        "h_feedback": any("证据解释" in json.dumps(s.get("output") or {}, ensure_ascii=False) for s in steps),
        "refs": refs, "explored_outside": sorted(outside), "used_ref": sorted(outside & set(refs)),
        "graph_tools": {k: tools.get(k, 0) for k in ("find", "neighbors", "path")},
        "first_cause": (f"{first.get('type')}:{json.dumps(first.get('entity'), ensure_ascii=False)}:{first.get('label')}"
                        if first else None),
        "mechanism": (score.get("mechanism") or {}).get("reason") if not score.get("primary_hit") else "命中",
        "cost": float(item["cost"]["total_cny"]), "seconds": item["seconds"],
        "model_requests": budget.get("model_requests"), "input_tokens": budget.get("input_tokens"),
        "output_tokens": budget.get("output_tokens"), "error": (run.get("error") or "")[:120],
    }


def deviation(f: dict) -> str:
    if f["passed"]:
        return "—"
    if f["status"] == "clarify":
        return "范围（待澄清）"
    if f["status"] != "succeeded":
        return "基础设施" if f["infra"] else f"未完成（{f['error'][:40]}）"
    if f["category"] in ("negative",) or (f["category"] == "adversarial" and f["primary"] is None):
        return "结论（反例 / 对抗）" if not f["policy"] else "其他"
    if f["primary"] is False:
        return "证据解释" if f["explored_outside"] else "证据未发现"
    if f["policy"] is False:
        return "结论"
    return "报告绑定"


def main() -> int:
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    rows = [features(r) for r in data["results"]]
    for r in rows:
        r["deviation"] = deviation(r)
    out = Path(sys.argv[1]).with_suffix(".summary.json")
    out.write_text(json.dumps({"meta": data.get("meta"), "ledger": data.get("ledger"), "stop_reason": data.get("stop_reason"),
                               "rows": rows}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    by = collections.defaultdict(list)
    for r in rows:
        by[r["condition"]].append(r)
    print("| 条件 | 运行 | 通过 | 首要原因命中 | 关键数值命中 | 修订 | 读了闭包外的表 | 用到参考表 | 费用（元） | 耗时中位（秒） |")
    for c in ("B0", "R", "H", "RH"):
        xs = by.get(c, [])
        if not xs:
            continue
        secs = sorted(x["seconds"] for x in xs)
        print(f"| {c} | {len(xs)} | {sum(x['passed'] for x in xs)} | {sum(bool(x['primary']) for x in xs)} | "
              f"{sum(bool(x['key']) for x in xs)} | {sum(bool(x['revised']) for x in xs)} | "
              f"{sum(bool(x['explored_outside']) for x in xs)} | {sum(bool(x['used_ref']) for x in xs)} | "
              f"{sum(x['cost'] for x in xs):.3f} | {secs[len(secs) // 2]} |")
    grid = collections.defaultdict(list)
    for r in rows:
        grid[(r["scenario"], r["condition"])].append(r)
    print()
    print("| 场景 | B0 | R | H | RH |")
    for s in dict.fromkeys(r["scenario"] for r in rows):
        cells = []
        for c in ("B0", "R", "H", "RH"):
            xs = grid.get((s, c), [])
            cells.append(f"{sum(x['passed'] for x in xs)}/{len(xs)} · 首因 {sum(bool(x['primary']) for x in xs)}")
        print(f"| {s} | " + " | ".join(cells) + " |")
    print()
    print("偏离计数：", dict(collections.Counter((r["condition"], r["deviation"]) for r in rows)))
    print("合计费用", round(sum(r["cost"] for r in rows), 4), "账本", data.get("ledger"), "停止", data.get("stop_reason"))
    print("摘要：", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
