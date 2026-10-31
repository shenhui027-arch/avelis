"""用评分器 v2 重评已保存的评测结果（不调用模型、不连数据库），旧 / 新分数并列。

  uv run --project backend python experiments/agent-v2/rescore_v2.py --eval-dir <.demo/eval> --truth <.demo/ec-truth.json> \
      --out experiments/agent-v2/rescore

对目录里每个带 runs 的评测文件（*-rescored.json 只有结果、没有运行详情，跳过）逐次运行判分：v1（tests/eval/scoring.py，
按当前题目定义）与 v2（tests/eval/scoring_v2.py），并写出逐次差异原因。保存时的 v1 分数（文件里的 results）也列出，
便于区分“题目定义后来改过”与“评分规则不同”两类差异。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend" / "src"), str(ROOT / "scripts")]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--eval-dir", type=Path, default=ROOT / ".demo" / "eval")
    parser.add_argument("--truth", type=Path, default=ROOT / ".demo" / "ec-truth.json")
    parser.add_argument("--out", type=Path, default=ROOT / "experiments" / "agent-v2" / "rescore")
    args = parser.parse_args()

    from tests.eval import scoring_v2

    truth = json.loads(args.truth.read_text(encoding="utf-8"))
    questions = {q["id"]: q for q in scoring_v2.load_questions(truth=truth)["questions"]}
    args.out.mkdir(parents=True, exist_ok=True)
    totals, lines = {}, ["# v1 / v2 重评", "", f"评分器 v2 {scoring_v2.VERSION}；真值 {args.truth.name}", ""]
    for path in sorted(args.eval_dir.glob("*.json")):
        saved = json.loads(path.read_text(encoding="utf-8"))
        if "runs" not in saved:
            continue
        stored = {(r["id"], r["repeat"]): r for r in saved["results"]}
        rows = []
        for item in saved["runs"]:
            q = questions.get(item["id"])
            if q is None:
                continue
            c = scoring_v2.compare(q, item["run"])
            c.update(repeat=item["repeat"], stored_v1_passed=stored[(item["id"], item["repeat"])]["passed"],
                     run_id=(item["run"] or {}).get("id"))
            rows.append(c)
        n = len(rows)
        summary = {"runs": n, "stored_v1": sum(r["stored_v1_passed"] for r in rows),
                   "v1_now": sum(r["v1_passed"] for r in rows), "v2": sum(r["v2_passed"] for r in rows),
                   "v2_policy": sum(r["v2_policy_passed"] for r in rows)}
        totals[path.name] = summary
        (args.out / f"{path.stem}.v2.json").write_text(json.dumps({"source": path.name, "summary": summary, "rows": rows},
                                                                  ensure_ascii=False, indent=2, default=str),
                                                       encoding="utf-8")
        lines += [f"## {path.name}", "",
                  f"运行 {n} 次：保存时 v1 通过 {summary['stored_v1']}；按当前题目 v1 通过 {summary['v1_now']}；"
                  f"v2 端到端通过 {summary['v2']}；v2 政策一致 {summary['v2_policy']}", "",
                  "| 题 | 次 | 保存 v1 | 当前 v1 | v2 | v1 关键数值 | v2 关键数值 | 差异原因 |", "|---|---|---|---|---|---|---|---|"]
        mark = {True: "✓", False: "✗", None: "—"}
        for r in rows:
            reason = "；".join(r["reasons"]) or ("保存 v1 与当前 v1 不同：题目定义后来改过" if r["stored_v1_passed"] != r["v1_passed"]
                                                 else "")
            lines.append(f"| {r['id']} | {r['repeat']} | {mark[r['stored_v1_passed']]} | {mark[r['v1_passed']]} | "
                         f"{mark[r['v2_passed']]} | {mark[r['v1_key']]} | {mark[r['v2_key']]} | "
                         f"{reason.replace('|', '/')[:400]} |")
        lines.append("")
    (args.out / "summary.json").write_text(json.dumps(totals, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(totals, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
