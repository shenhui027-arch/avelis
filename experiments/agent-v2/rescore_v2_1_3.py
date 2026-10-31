"""并列重评 v2.1.1 与 v2.1.3 的已保存运行，不调用模型、不连数据库。

沿用 --eval-dir、--e1 等参数，--runs 可另给多个 E1 / E2 的 results 文件。
人工审定只用于 E1，weak 与未列出的 E1 运行均按 false 计。
每个输入文件独立保存判分、变化原因与截断不确定的运行计数。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rescore_v2_1_1 import ROOT, _detail, _dump, _mark  # noqa: E402
from rescore_v2_1_1 import audit_primary as _audit_primary_v211  # noqa: E402
from rescore_v2_1_2 import _is_e1  # noqa: E402


def change_reasons(old: dict, new: dict) -> list[str]:
    """逐项解释通过、首因或关键数值变化。"""
    reasons = []
    if old["primary_hit"] != new["primary_hit"]:
        reasons.append(f"首要原因 {old['primary_hit']} → {new['primary_hit']}："
                       f"v2.1.1 {_detail(old, 'primary_detail')}；v2.1.3 {_detail(new, 'primary_detail')}")
    if old["key_value_hit"] != new["key_value_hit"]:
        reasons.append(f"关键数值 {old['key_value_hit']} → {new['key_value_hit']}："
                       f"v2.1.1 {_detail(old, 'key_value_detail')}；v2.1.3 {_detail(new, 'key_value_detail')}")
    if old["passed"] != new["passed"]:
        reasons.insert(0, f"通过 {old['passed']} → {new['passed']}")
        if old.get("audit") != new.get("audit"):
            reasons.append(f"权限审计：v2.1.1 {_detail(old, 'audit')}；v2.1.3 {_detail(new, 'audit')}")
    return reasons


def _has_truncated(result: dict) -> bool:
    return any((result.get(field) or {}).get("uncertain_truncated", 0) > 0
               for field in ("primary_detail", "key_value_detail"))


def _truncation_changed(question: dict, run: dict | None, new: dict) -> bool:
    """用新增规则之前的分数作对照，避免把其他版本差异归因于截断。"""
    if not _has_truncated(new):
        return False
    from tests.eval import scoring_v2_1_2

    without_rule = scoring_v2_1_2.score(question, run)
    return any(without_rule[field] != new[field] for field in ("passed", "primary_hit", "key_value_hit"))


def score_file(saved: dict, source: str, questions: dict, questions_v213: dict, scorer_v211, scorer_v213) -> dict:
    """两份题目独立加载，避免两版评分器之间共享可变题目记录。"""
    items = saved.get("runs") if "runs" in saved else saved.get("results")
    rows, skipped = [], []
    meta = saved.get("meta") or {}
    for item in items or []:
        scenario = item.get("scenario") or item.get("id")
        if scenario not in questions or scenario not in questions_v213:
            skipped.append({"scenario": scenario, "repeat": item.get("repeat"), "reason": "题目规格里没有该题"})
            continue
        if "run" not in item:
            skipped.append({"scenario": scenario, "repeat": item.get("repeat"), "reason": "没有保存运行详情"})
            continue
        condition = (item.get("condition") or saved.get("condition") or meta.get("condition")
                     or Path(source.split(":", 1)[-1]).stem)
        repeat = item.get("repeat", 1)
        run = item["run"]
        old = scorer_v211.score(questions[scenario], run)
        new = scorer_v213.score(questions_v213[scenario], run)
        rows.append({"id": scenario, "scenario": scenario, "condition": condition, "repeat": repeat,
                     "audit_key": f"{scenario}:{condition}:{repeat}", "run_id": (run or {}).get("id"),
                     "source": source, "v2_1_1": old, "v2_1_3": new, "reasons": change_reasons(old, new),
                     "uncertain_truncated_changed": _truncation_changed(questions_v213[scenario], run, new)})
    return {"source": source, "summary": summarize(rows), "rows": rows, "skipped": skipped}


def summarize(rows: list[dict]) -> dict:
    """分开统计可判项与命中项，manual 的 null 不算错误也不算命中。"""
    result = {"runs": len(rows), "changed": sum(bool(r["reasons"]) for r in rows),
              "uncertain_truncated_runs": sum(_has_truncated(r["v2_1_3"]) for r in rows),
              "uncertain_truncated_changed": sum(r["uncertain_truncated_changed"] for r in rows)}
    for version in ("v2_1_1", "v2_1_3"):
        result[version] = {field: {"hits": sum(r[version][field] is True for r in rows),
                                  "scored": sum(r[version][field] is not None for r in rows)}
                           for field in ("passed", "primary_hit", "key_value_hit")}
    return result


def audit_primary(rows: list[dict], audit: dict) -> dict:
    """只审定 E1，复用未列出按 false、同键去重与冲突校验。"""
    e1_rows = [{**row, "v2_1_1": row["v2_1_3"]}
               for row in rows if row["source"].startswith("e1:")]
    return _audit_primary_v211(e1_rows, audit)


def markdown(files: list[dict], summary: dict) -> str:
    lines = ["# v2.1.1 / v2.1.3 重评", "", f"运行 {summary['overall']['runs']} 次；"
             f"通过、首因或关键数值判定变化 {summary['overall']['changed']} 次。",
             f"出现截断不确定的运行 {summary['overall']['uncertain_truncated_runs']} 次，"
             f"其中因截断规则改变判定 {summary['overall']['uncertain_truncated_changed']} 次。", "",
             "评分器与题目规格 SHA256：", ""]
    lines += [f"- `{name}`：`{digest}`" for name, digest in summary["sha256"].items()]
    lines += ["", "截断导致的判定变化以新增规则前的 v2.1.2 为内部对照，比较通过、首因和关键数值；"
              "不把 v2.1.1 到 v2.1.2 的变化计入截断影响。"]
    for saved in files:
        lines += ["", f"## {saved['source']}", "",
                  f"出现截断不确定的运行 {saved['summary']['uncertain_truncated_runs']} 次，"
                  f"其中因截断规则改变判定 {saved['summary']['uncertain_truncated_changed']} 次。", "",
                  "| 运行 | v2.1.1 通过 | v2.1.3 通过 | v2.1.1 首因 | v2.1.3 首因 | v2.1.1 数值 | v2.1.3 数值 |",
                  "|---|---|---|---|---|---|---|"]
        for row in saved["rows"]:
            old, new = row["v2_1_1"], row["v2_1_3"]
            cells = [_mark(result[field]) for field in ("passed", "primary_hit", "key_value_hit")
                     for result in (old, new)]
            lines.append(f"| {row['audit_key']} | " + " | ".join(cells) + " |")
        changes = [r for r in saved["rows"] if r["reasons"]]
        if changes:
            lines += ["", "判定变化：", ""]
            for row in changes:
                lines.append(f"- `{row['audit_key']}`：" + "；".join(row["reasons"]))
        if saved["skipped"]:
            lines += ["", "跳过的记录：", ""]
            lines += [f"- `{r['scenario']}:{r['repeat']}`：{r['reason']}" for r in saved["skipped"]]
    audit = summary["audit"]
    lines += ["", "## 首要原因与人工审定", ""]
    if audit is None:
        lines.append("未提供 --audit，未比较人工审定。")
    else:
        lines.append(f"纳入 {audit['matched']} 个 E1 审定键；weak 与未列出的 E1 均按 false 计，"
                     "重复来源的同一审定键只数一次。")
        for field, title in (("original", "原审定"), ("corrected", "更正审定")):
            item = audit[field]
            lines += ["", f"{title}：可判 {item['scored']} 次；假阳性 {item['false_positive_count']} 次；"
                      f"假阴性 {item['false_negative_count']} 次。", ""]
            for name, label in (("false_positives", "假阳性"), ("false_negatives", "假阴性")):
                lines.append(f"- {label}：" + ("；".join(f"`{r['key']}`（{r['note']}）" for r in item[name]) or "无"))
            if item["unscored"]:
                lines.append("- 首因未自动判定：" + "、".join(f"`{r['key']}`" for r in item["unscored"]))
        if audit["unmatched_audit_keys"]:
            lines += ["", "未匹配到 E1 运行的审定键：" + "、".join(f"`{k}`" for k in audit["unmatched_audit_keys"])]
        if audit["unreviewed_run_keys"]:
            lines += ["", f"另有 {len(audit['unreviewed_run_keys'])} 个 E1 运行键未列出人工审定，"
                      "已按 false 计入，清单见 summary.json。"]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="并列重评已保存运行的 v2.1.1 / v2.1.3 分数")
    parser.add_argument("--eval-dir", type=Path, default=ROOT / ".demo" / "eval")
    parser.add_argument("--truth", type=Path, default=ROOT / ".demo" / "ec-truth.json")
    parser.add_argument("--out", type=Path, default=ROOT / "experiments" / "agent-v2" / "rescore_v2_1_3")
    parser.add_argument("--e1", type=Path, help="E1 结果 JSON（results 内含 scenario、condition、repeat、run）")
    parser.add_argument("--runs", type=Path, nargs="+", default=[], help="多个 E1 / E2 结构的 results JSON 文件")
    parser.add_argument("--audit", type=Path, help="仅用于 E1 的人工审定 YAML（runs 映射）")
    args = parser.parse_args()

    from tests.eval import scoring_v2_1_1, scoring_v2_1_3

    hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
              for path in (Path(scoring_v2_1_3.__file__), scoring_v2_1_3.SPEC)}
    for name, digest in hashes.items():
        print(f"sha256 {name} {digest}", flush=True)
    truth = json.loads(args.truth.read_text(encoding="utf-8"))
    questions = {q["id"]: q for q in scoring_v2_1_1.load_questions(truth=truth)["questions"]}
    questions_v213 = {q["id"]: q for q in scoring_v2_1_3.load_questions(truth=truth)["questions"]}
    sources = [("eval", path) for path in sorted(args.eval_dir.glob("*.json"))]
    if args.e1:
        sources.append(("e1", args.e1))
    sources.extend(("runs", path) for path in args.runs)
    files = []
    for kind, path in sources:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if kind == "eval" and "runs" not in saved:
            continue
        if kind == "runs" and _is_e1(saved, path):
            kind = "e1"
        source = f"{kind}:{path}"
        files.append(score_file(saved, source, questions, questions_v213, scoring_v2_1_1, scoring_v2_1_3))
    rows = [row for saved in files for row in saved["rows"]]
    audit = None
    if args.audit:
        import yaml

        audit = audit_primary(rows, (yaml.safe_load(args.audit.read_text(encoding="utf-8")) or {}).get("runs") or {})
    summary = {"sha256": hashes, "truth": args.truth.name, "overall": summarize(rows),
               "files": {saved["source"]: saved["summary"] for saved in files}, "audit": audit,
               "changes": [row for row in rows if row["reasons"]]}
    args.out.mkdir(parents=True, exist_ok=True)
    for index, saved in enumerate(files, 1):
        kind, filename = saved["source"].split(":", 1)
        # 序号避免不同目录里的同名输入覆盖彼此的完整判分。
        output = args.out / f"{index:03d}-{kind}-{Path(filename).stem}.v2_1_3.json"
        output.write_text(_dump(saved) + "\n", encoding="utf-8")
    (args.out / "summary.json").write_text(_dump(summary) + "\n", encoding="utf-8")
    (args.out / "summary.md").write_text(markdown(files, summary), encoding="utf-8")
    print(_dump(summary["overall"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
