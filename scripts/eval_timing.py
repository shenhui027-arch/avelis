"""分析评测的耗时拆解（ADR 0012 度量）：读评测结果 JSON 里每次运行的步骤，按阶段拆开耗时与模型调用。

在仓库根目录运行：
  uv run --project backend python scripts/eval_timing.py .demo/eval/<时间>.json [输出.md]

阶段按步骤顺序划分：
- 理解：第一个 understand 步骤之前的模型调用；
- 预检：understand 之后、下一次模型调用之前的工具步骤（系统执行，不经模型）；
- 推理（下钻）：之后不含 submit_report 的模型轮次及其工具调用；
- 提交报告：发出 submit_report 的模型轮次（含参数解析失败后的重交）；
- 修订：第一次提交报告之后的全部轮次（核验没过、给一轮修订）。
输出两张表：逐题表，以及按问题类别统计的中位数与 P90。用来判断时间花在问题本身需要的下钻，还是流程浪费
（简单问题也走多轮、报告一轮输出过长等）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PHASES = ("理解", "预检", "推理", "报告", "修订")


def phases(steps: list[dict]) -> dict[str, dict[str, float]]:
    """一次运行按阶段汇总：秒、模型调用、输出 token、推理 token、工具调用。"""
    out = {p: {"seconds": 0.0, "model_calls": 0, "output": 0, "reasoning": 0, "tools": 0} for p in PHASES}
    phase, reported = "理解", False
    for s in sorted(steps, key=lambda s: s.get("seq") or 0):
        kind, tool, o = s.get("kind"), s.get("tool"), s.get("output") or {}
        if kind == "plan" and tool == "understand":
            phase = "预检"
            continue
        if kind == "note" and tool == "submit_report" and s.get("status") == "ok":
            reported = True
            continue
        if kind == "model":
            if reported:
                phase = "修订"
            elif phase != "理解":
                phase = "报告" if "submit_report" in (o.get("tools") or []) else "推理"
            out[phase]["model_calls"] += 1
            out[phase]["output"] += o.get("output_tokens") or 0
            out[phase]["reasoning"] += o.get("reasoning_tokens") or 0
        elif kind == "tool" and tool != "submit_report":
            out[phase]["tools"] += 1
        out[phase]["seconds"] += (s.get("duration_ms") or 0) / 1000
    return out


def _spread(values: list[float]) -> str:
    vals = sorted(values)
    if not vals:
        return "—"
    mid = len(vals) // 2
    median = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
    p90 = vals[max(0, -(-len(vals) * 9 // 10) - 1)]
    return f"{median:.1f} / {p90:.1f}"


def report(data: dict) -> str:
    category = {(r["id"], r["repeat"]): r for r in data.get("results") or []}
    rows = []
    for item in data.get("runs") or []:
        run = item.get("run") or {}
        result = category.get((item["id"], item["repeat"]), {})
        p = phases(run.get("steps") or [])
        total = ((run.get("budget") or {}).get("used") or {}).get("seconds")
        rows.append({"id": item["id"], "repeat": item["repeat"], "category": result.get("category", "—"),
                     "passed": result.get("passed"), "total": total, "phases": p,
                     "model_calls": sum(v["model_calls"] for v in p.values()),
                     "output": sum(v["output"] for v in p.values()), "reasoning": sum(v["reasoning"] for v in p.values()),
                     "drill_tools": p["推理"]["tools"] + p["修订"]["tools"]})
    lines = ["## 逐题耗时", "",
             "| 题 | 类别 | 通过 | 总秒 | 理解秒 | 预检秒 | 推理秒（轮） | 报告秒（轮） | 修订秒（轮） | 模型调用 | 输出 token | 其中推理 | 下钻工具 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        p = r["phases"]
        lines.append(f"| {r['id']} | {r['category']} | {'✓' if r['passed'] else '✗'} | {r['total']} | "
                     f"{p['理解']['seconds']:.1f} | {p['预检']['seconds']:.1f} | "
                     f"{p['推理']['seconds']:.1f}（{p['推理']['model_calls']}） | {p['报告']['seconds']:.1f}（{p['报告']['model_calls']}） | "
                     f"{p['修订']['seconds']:.1f}（{p['修订']['model_calls']}） | {r['model_calls']} | {r['output']} | "
                     f"{r['reasoning']} | {r['drill_tools']} |")
    lines += ["", "## 按类别：中位数 / P90", "",
              "| 类别 | 次数 | 总秒 | 推理秒 | 报告秒 | 修订秒 | 模型调用 | 输出 token | 其中推理 | 下钻工具 |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["category"], []).append(r)
    groups["全部"] = rows
    for name, rs in groups.items():
        lines.append(f"| {name} | {len(rs)} | {_spread([r['total'] for r in rs if r['total'] is not None])} | "
                     f"{_spread([r['phases']['推理']['seconds'] for r in rs])} | "
                     f"{_spread([r['phases']['报告']['seconds'] for r in rs])} | "
                     f"{_spread([r['phases']['修订']['seconds'] for r in rs])} | "
                     f"{_spread([r['model_calls'] for r in rs])} | {_spread([r['output'] for r in rs])} | "
                     f"{_spread([r['reasoning'] for r in rs])} | {_spread([r['drill_tools'] for r in rs])} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if "runs" not in data:  # 单次运行详情（验收留存的 analysis-run-*.json）
        data = {"runs": [{"id": data.get("id", "run")[:8], "repeat": 1, "run": data}], "results": []}
    text = report(data)
    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
