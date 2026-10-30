"""uv run --project backend python experiments/agent-v2/e1_summary.py <.demo/experiments/e1/结果.json>

E1 结果汇总（诊断用）：每个场景 × 条件的 v2 / v1 判分、状态、注入字符数、费用、耗时，以及判断第一处偏离要用的轨迹特征。"""
import collections
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend" / "src")]
from tests.eval import scoring as v1
from tests.eval import scoring_v2 as v2

path = sys.argv[1]
d = json.load(open(path, encoding="utf-8"))
qs = {q["id"]: q for q in v1.load_questions()["questions"]}
EVENT = re.compile(r"ec_marketing\.campaign|ec_product\.price_change|marketing_daily|ad_spend|campaign|price_change", re.I)
rows = []
for r in d["results"]:
    run = r.get("run") or {}
    q = qs[r["scenario"]]
    s1 = v1.score(q, run)
    s2 = r["score"]
    steps = run.get("steps") or []
    plan_scope = ((run.get("plan") or {}).get("scope") or {})
    model_scope = (run.get("plan") or {}).get("model_scope")
    rep = run.get("report") or {}
    causes = rep.get("causes") or []
    first = causes[0] if causes else {}
    agent_steps = [s for s in steps if s["kind"] == "tool" and "实验注入" not in (s.get("reason") or "")]
    event_q = sum(1 for s in agent_steps if s["tool"] in ("query_sql", "query_metric", "describe_asset")
                  and EVENT.search(json.dumps(s.get("args"), ensure_ascii=False)))
    inj = r.get("injection") or {}
    budget = (run.get("budget") or {}).get("used") or {}
    rows.append({
        "scenario": r["scenario"], "cond": r["condition"], "rep": r["repeat"], "status": run.get("status"),
        "v2": s2["passed"], "v1": s1["passed"], "policy": s2["policy_conclusion_ok"], "primary": s2["primary_hit"],
        "key": s2["key_value_hit"], "src_fail": s2["sources_failed"], "concl": rep.get("conclusion"),
        "scope_metrics": plan_scope.get("metric_codes"), "scope_filters": plan_scope.get("filters"),
        "model_scope": (model_scope or {}).get("metric_codes") if model_scope else None,
        "first_cause": f"{first.get('type')}:{json.dumps(first.get('entity'), ensure_ascii=False)}:{first.get('label')}:{first.get('direction')}:{first.get('status')}" if first else None,
        "agent_event_queries": event_q, "injected_chars": inj.get("injected_chars"), "inject_failed": inj.get("failed"),
        "cost": r["cost"]["total_cny"], "seconds": r["seconds"], "model_calls": budget.get("model_requests"),
        "in_tok": budget.get("input_tokens"), "out_tok": budget.get("output_tokens"),
        "stopped": run.get("error") or ((rep.get("notes") or [])[-1:] or [None])[0],
        "key_reason": (s2.get("key_value_detail") or {}).get("reason"),
        "clarify": run.get("status") == "clarify", "clar_q": (rep.get("clarification") or {}).get("question"),
    })
json.dump(rows, open(path.replace(".json", ".summary.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
grid = collections.defaultdict(list)
for x in rows:
    grid[(x["scenario"], x["cond"])].append(x)
print("| 场景 | 条件 | v2 通过 | v1 通过 | 状态 | 首要原因 | 关键数值 | 事件查询 | 注入字符 | 费用 | 秒 |")
for (s, c), xs in sorted(grid.items()):
    print(f"| {s} | {c} | {sum(x['v2'] for x in xs)}/{len(xs)} | {sum(x['v1'] for x in xs)}/{len(xs)} | "
          f"{','.join(str(x['status']) for x in xs)} | {','.join(str(x['primary']) for x in xs)} | {','.join(str(x['key']) for x in xs)} | "
          f"{','.join(str(x['agent_event_queries']) for x in xs)} | {','.join(str(x['injected_chars']) for x in xs)} | "
          f"{sum(float(x['cost']) for x in xs):.4f} | {','.join(str(x['seconds']) for x in xs)} |")
by_c = collections.defaultdict(list)
for x in rows:
    by_c[x["cond"]].append(x)
for c, xs in sorted(by_c.items()):
    print(c, "v2", sum(x["v2"] for x in xs), "/", len(xs), "v1", sum(x["v1"] for x in xs), "primary", sum(bool(x["primary"]) for x in xs),
          "clarify", sum(x["clarify"] for x in xs), "cost", round(sum(float(x["cost"]) for x in xs), 4),
          "sec med", sorted(x["seconds"] for x in xs)[len(xs) // 2])
print("total cost", round(sum(float(x["cost"]) for x in rows), 4), "ledger", d.get("ledger"), "stop", d.get("stop_reason"))
