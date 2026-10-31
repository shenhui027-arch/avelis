"""并列重评 v2.1 与 v2.1.1 的已保存运行，不调用模型、不连数据库。

支持 --eval-dir 的 runs 文件，以及 --e1 的 results 文件；只有用户显式执行 main 才读取输入。
逐文件保存完整判分、机制明细与变化原因，汇总对原审定和更正审定的首要原因误判。
人工审定键为 scenario:condition:repeat；weak 按 false 计，未列出的 E1 运行也按 false 计。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rescore_v2_1 import ROOT, _detail, _dump, _mark  # noqa: E402
from rescore_v2_1 import _human as _human_v21  # noqa: E402


def change_reasons(old: dict, new: dict) -> list[str]:
    """逐项解释通过、首因或关键数值变化，并保留权限门槛的变化原因。"""
    reasons = []
    if old["primary_hit"] != new["primary_hit"]:
        reasons.append(f"首要原因 {old['primary_hit']} → {new['primary_hit']}："
                       f"v2.1 {_detail(old, 'primary_detail')}；v2.1.1 {_detail(new, 'primary_detail')}")
    if old["key_value_hit"] != new["key_value_hit"]:
        reasons.append(f"关键数值 {old['key_value_hit']} → {new['key_value_hit']}："
                       f"v2.1 {_detail(old, 'key_value_detail')}；v2.1.1 {_detail(new, 'key_value_detail')}")
    if old["passed"] != new["passed"]:
        reasons.insert(0, f"通过 {old['passed']} → {new['passed']}")
        if old.get("audit") != new.get("audit"):
            reasons.append(f"权限审计：v2.1 {_detail(old, 'audit')}；v2.1.1 {_detail(new, 'audit')}")
        if (old["passed"] and not new["passed"] and not new.get("completed")
                and old.get("denial") and not (new.get("denial") or {}).get("lost")):
            reasons.append("未完成运行缺少因权限结束的 lost 标记，单步 denials 不再算正确拒绝")
    return reasons


def score_file(saved: dict, source: str, questions: dict, questions_v211: dict, scorer_v21, scorer_v211) -> dict:
    """两份题目独立加载，避免两版评分器之间共享可变题目记录。"""
    items = saved.get("runs") if "runs" in saved else saved.get("results")
    rows, skipped = [], []
    meta = saved.get("meta") or {}
    for item in items or []:
        scenario = item.get("scenario") or item.get("id")
        if scenario not in questions or scenario not in questions_v211:
            skipped.append({"scenario": scenario, "repeat": item.get("repeat"), "reason": "题目规格里没有该题"})
            continue
        if "run" not in item:
            skipped.append({"scenario": scenario, "repeat": item.get("repeat"), "reason": "没有保存运行详情"})
            continue
        condition = (item.get("condition") or saved.get("condition") or meta.get("condition")
                     or Path(source.split(":", 1)[-1]).stem)
        repeat = item.get("repeat", 1)
        run = item["run"]
        old = scorer_v21.score(questions[scenario], run)
        new = scorer_v211.score(questions_v211[scenario], run)
        rows.append({"id": scenario, "scenario": scenario, "condition": condition, "repeat": repeat,
                     "audit_key": f"{scenario}:{condition}:{repeat}", "run_id": (run or {}).get("id"),
                     "source": source, "v2_1": old, "v2_1_1": new, "reasons": change_reasons(old, new)})
    return {"source": source, "summary": summarize(rows), "rows": rows, "skipped": skipped}


def summarize(rows: list[dict]) -> dict:
    """分开统计可判项与命中项，manual 的 null 不算错误也不算命中。"""
    result = {"runs": len(rows), "changed": sum(bool(r["reasons"]) for r in rows)}
    for version in ("v2_1", "v2_1_1"):
        result[version] = {field: {"hits": sum(r[version][field] is True for r in rows),
                                  "scored": sum(r[version][field] is not None for r in rows)}
                           for field in ("passed", "primary_hit", "key_value_hit")}
    return result


def _human(value: object, key: str, field: str) -> bool:
    # 两份审定里的 weak 均按 false 计，其余值沿用 v2.1 的校验。
    return False if value == "weak" else _human_v21(value, key, field)


def audit_primary(rows: list[dict], audit: dict) -> dict:
    """未列出的 E1 按 false 计；同键去重，重复来源的首因冲突时显式报错。"""
    implicit = {row["audit_key"] for row in rows
                if row["source"].startswith("e1:") and row["audit_key"] not in audit}
    reviews = {**audit, **{key: {"original": False, "corrected": False,
                               "note": "未列出的 E1 运行，按 false 计"} for key in implicit}}
    unique = {}
    for row in rows:
        key = row["audit_key"]
        if key not in reviews:
            continue
        hit = row["v2_1_1"]["primary_hit"]
        if key in unique and unique[key]["primary_hit"] != hit:
            raise ValueError(f"审定键 {key} 对应的重复运行 primary_hit 不一致，请明确输入来源")
        item = unique.setdefault(key, {"key": key, "primary_hit": hit, "sources": [],
                                       "note": reviews[key].get("note", "")})
        if row["source"] not in item["sources"]:
            item["sources"].append(row["source"])
    result = {"matched": len(unique), "unmatched_audit_keys": sorted(set(audit) - set(unique)),
              "unreviewed_run_keys": sorted({r["audit_key"] for r in rows} - set(audit))}
    for field in ("original", "corrected"):
        false_positive, false_negative, unscored = [], [], []
        for key, item in unique.items():
            human = _human(reviews[key].get(field), key, field)
            entry = {**item, "human": human, "recorded": reviews[key][field]}
            if item["primary_hit"] is None:
                unscored.append(entry)
            elif item["primary_hit"] is True and not human:
                false_positive.append(entry)
            elif item["primary_hit"] is False and human:
                false_negative.append(entry)
        result[field] = {"scored": len(unique) - len(unscored),
                         "false_positive_count": len(false_positive), "false_positives": false_positive,
                         "false_negative_count": len(false_negative), "false_negatives": false_negative,
                         "unscored": unscored}
    return result


def markdown(files: list[dict], summary: dict) -> str:
    lines = ["# v2.1 / v2.1.1 重评", "", f"运行 {summary['overall']['runs']} 次；"
             f"通过、首因或关键数值判定变化 {summary['overall']['changed']} 次。", "",
             "评分器与题目规格 SHA256：", ""]
    lines += [f"- `{name}`：`{digest}`" for name, digest in summary["sha256"].items()]
    for saved in files:
        lines += ["", f"## {saved['source']}", "",
                  "| 运行 | v2.1 通过 | v2.1.1 通过 | v2.1 首因 | v2.1.1 首因 | v2.1 数值 | v2.1.1 数值 |",
                  "|---|---|---|---|---|---|---|"]
        for row in saved["rows"]:
            old, new = row["v2_1"], row["v2_1_1"]
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
        lines.append(f"纳入 {audit['matched']} 个审定键；weak 与未列出的 E1 均按 false 计，"
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
            lines += ["", "未匹配到运行的审定键：" + "、".join(f"`{k}`" for k in audit["unmatched_audit_keys"])]
        if audit["unreviewed_run_keys"]:
            lines += ["", f"另有 {len(audit['unreviewed_run_keys'])} 个运行键未列出人工审定，"
                      "其中 E1 已按 false 计入，清单见 summary.json。"]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="并列重评已保存运行的 v2.1 / v2.1.1 分数")
    parser.add_argument("--eval-dir", type=Path, default=ROOT / ".demo" / "eval")
    parser.add_argument("--truth", type=Path, default=ROOT / ".demo" / "ec-truth.json")
    parser.add_argument("--out", type=Path, default=ROOT / "experiments" / "agent-v2" / "rescore_v2_1_1")
    parser.add_argument("--e1", type=Path, help="E1 结果 JSON（results 内含 scenario、condition、repeat、run）")
    parser.add_argument("--audit", type=Path, help="人工审定 YAML（runs 映射）")
    args = parser.parse_args()

    from tests.eval import scoring_v2_1, scoring_v2_1_1

    hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
              for path in (Path(scoring_v2_1_1.__file__), scoring_v2_1_1.SPEC)}
    for name, digest in hashes.items():
        print(f"sha256 {name} {digest}", flush=True)
    truth = json.loads(args.truth.read_text(encoding="utf-8"))
    questions = {q["id"]: q for q in scoring_v2_1.load_questions(truth=truth)["questions"]}
    questions_v211 = {q["id"]: q for q in scoring_v2_1_1.load_questions(truth=truth)["questions"]}
    sources = [(f"eval:{path.name}", path) for path in sorted(args.eval_dir.glob("*.json"))]
    if args.e1:
        sources.append((f"e1:{args.e1.name}", args.e1))
    files = []
    for source, path in sources:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if source.startswith("eval:") and "runs" not in saved:
            continue
        files.append(score_file(saved, source, questions, questions_v211, scoring_v2_1, scoring_v2_1_1))
    rows = [row for saved in files for row in saved["rows"]]
    audit = None
    if args.audit:
        import yaml

        audit = audit_primary(rows, (yaml.safe_load(args.audit.read_text(encoding="utf-8")) or {}).get("runs") or {})
    summary = {"sha256": hashes, "truth": args.truth.name, "overall": summarize(rows),
               "files": {saved["source"]: saved["summary"] for saved in files}, "audit": audit,
               "changes": [row for row in rows if row["reasons"]]}
    args.out.mkdir(parents=True, exist_ok=True)
    for saved in files:
        kind, filename = saved["source"].split(":", 1)
        (args.out / f"{kind}-{Path(filename).stem}.v2_1_1.json").write_text(_dump(saved) + "\n", encoding="utf-8")
    (args.out / "summary.json").write_text(_dump(summary) + "\n", encoding="utf-8")
    (args.out / "summary.md").write_text(markdown(files, summary), encoding="utf-8")
    print(_dump(summary["overall"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
