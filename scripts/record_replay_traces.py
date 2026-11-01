"""从真实模型的分析运行录制回放轨迹（ADR 0009 第 5 点）：写到 backend/tests/replay/traces/<名称>.json。

  uv run --project backend python scripts/record_replay_traces.py --eval .demo/eval/<时间>.json   从评测结果里各挑一条：
                                                         首轮通过、修订后通过、修订后仍失败
  uv run --project backend python scripts/record_replay_traces.py --run first_pass=<运行编号> ...  指定运行

在跑过分析的元数据库上运行（与评测相同，AVELIS_DATABASE_URL 可指向单独的库）。按字段白名单保存
（见 backend/tests/replay/fake_model.py）：
- messages：模型每次回复的 content 与 tool_calls（name、args），取自 LangGraph checkpoint，不含模型元数据与工具结果原文；
- fixtures：取数类工具（受治理查询、自动归因、探索查询、元数据上下文）产生的数据集，只保留列、行与量纲等核验需要的字段，
  不保存 SQL、数据源与资产编号；计算类工具不保存产出，回放时用冻结的数据重新计算；
- expect：真实运行的核验结论（status、ok、revised、conclusion），回放必须得到同样的结论。
只录评测集里的题目（合成数据上的运行，不把生产运行转成回放）；写盘前检查轨迹不含 .env 里的口令与密钥、连接串。
找不到某一类运行时跳过它（保留原有轨迹文件），并打印原因。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "backend"), str(ROOT / "backend" / "src")]

import demo  # noqa: E402

KINDS = ("first_pass", "revised_pass", "revised_fail")


def pick(eval_path: Path) -> dict[str, list[str]]:
    """评测结果里各类运行（按出现顺序；录制时逐个尝试，第一条能原样回放的入选）。"""
    data = json.loads(eval_path.read_text(encoding="utf-8"))
    chosen: dict[str, list[str]] = {}
    for item in data["runs"]:
        run = item.get("run") or {}
        v = run.get("verification") or {}
        if run.get("status") != "succeeded" or not run.get("id") or v.get("ok") is None:
            continue
        kind = (
            ("revised_pass" if v["ok"] else "revised_fail") if v.get("revised") else ("first_pass" if v["ok"] else None)
        )
        if kind:
            chosen.setdefault(kind, []).append(run["id"])
    return chosen


def leaks(text: str, env: dict[str, str]) -> list[str]:
    """轨迹文本里出现的 .env 口令、密钥（只返回变量名）与连接串。"""
    secret = ("PASSWORD", "SECRET", "KEY", "TOKEN")
    found = [k for k, v in env.items() if any(w in k for w in secret) and len(v) >= 6 and v in text]
    return found + (["连接串"] if "://" in text else [])


def record(name: str, run_id: str, env: dict[str, str]) -> str | None:
    from langchain_core.messages import AIMessage
    from sqlalchemy import select

    from avelis.analysis import service
    from avelis.db import session_scope
    from avelis.models import AnalysisDataset, AnalysisRun, AnalysisStep
    from tests.eval.scoring import load_questions
    from tests.replay.fake_model import FETCH_TOOLS, TRACES, dataset_fixture

    eval_questions = {q["question"] for q in load_questions()["questions"]}

    with session_scope() as session:
        run = session.get(AnalysisRun, run_id)
        if run is None:
            return f"运行 {run_id} 不存在"
        if run.question not in eval_questions:
            return "不是评测集里的题目：只录合成数据上的评测运行"
        steps = list(
            session.scalars(
                select(AnalysisStep)
                .where(AnalysisStep.run_id == run_id, AnalysisStep.kind == "tool")
                .order_by(AnalysisStep.seq)
            )
        )
        datasets = {d.id: d for d in session.scalars(select(AnalysisDataset).where(AnalysisDataset.run_id == run_id))}
        by_seq = {d.seq: d for d in datasets.values()}
        if any(s.status == "denied" for s in steps):
            return "运行里有被拒绝的工具调用（越权或预算耗尽），回放的预算与真实运行不同"
        state = service._graph().get_state({"configurable": {"thread_id": f"analysis:{run_id}"}}).values
        replies = [m for m in state.get("messages", []) if isinstance(m, AIMessage)]
        if not replies:
            return "checkpoint 里没有模型回复"
        if any(getattr(m, "invalid_tool_calls", None) for m in replies):
            return "模型回复里有参数无法解析的工具调用，假模型无法原样回放"
        messages = []
        for m in replies:
            message = {
                "content": m.content if isinstance(m.content, str) else json.dumps(m.content, ensure_ascii=False)
            }
            if m.tool_calls:
                message["tool_calls"] = [{"name": c["name"], "args": c["args"]} for c in m.tool_calls]
            messages.append(message)
        fixtures, last = [], 0
        for step in steps:  # 每个有产出的步骤，产出是上一个产出之后、到它返回的数据集为止的全部数据集
            produced = []
            if step.dataset_id and step.dataset_id in datasets:
                seq = datasets[step.dataset_id].seq
                produced, last = [by_seq[i] for i in range(last + 1, seq + 1)], seq
            if step.tool in FETCH_TOOLS:
                fixtures.append({"tool": step.tool, "datasets": [dataset_fixture(d) for d in produced]})
            elif produced and (len(produced) != 1 or produced[0].kind != "calc"):
                return f"计算步骤 {step.seq}（{step.tool}）的产出不是单个计算数据集，无法确定回放顺序"
        if last != len(by_seq):
            return "有数据集不属于任何步骤，无法确定回放顺序"
        v = run.verification or {}
        from avelis.analysis.tools import REPORT_SCHEMA

        trace = {
            "version": 2,
            "report_schema": REPORT_SCHEMA,
            "name": name,
            "question": run.question,
            "window": run.window,
            "messages": messages,
            "fixtures": fixtures,
            "expect": {
                "status": v.get("status"),
                "ok": v.get("ok"),
                "revised": v.get("revised"),
                "conclusion": (run.report or {}).get("conclusion"),
            },
        }
    text = json.dumps(trace, ensure_ascii=False, indent=2, default=str) + "\n"
    leaked = leaks(text, env)
    if leaked:
        return f"轨迹含不应保存的内容：{'、'.join(leaked)}"
    path = TRACES / f"{name}.json"
    path.write_text(text, encoding="utf-8")
    print(f"{name}: 运行 {run_id} → {path.relative_to(ROOT)}（{len(messages)} 条模型回复，{len(fixtures)} 个取数步骤）")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="录制回放轨迹")
    parser.add_argument("--eval", type=Path, help="评测结果 JSON")
    parser.add_argument("--run", action="append", default=[], help="名称=运行编号")
    args = parser.parse_args()
    env = demo.load_env()
    for key, value in env.items():  # 已设置的环境变量（如单独的元数据库）优先
        if key.startswith("AVELIS_"):
            os.environ.setdefault(key, value)
    chosen = pick(args.eval) if args.eval else {}
    chosen.update({k: [v] for k, v in (r.split("=", 1) for r in args.run)})
    for name in KINDS:
        if not chosen.get(name):
            print(f"{name}: 没有找到这一类运行，保留原有轨迹")
            continue
        for run_id in chosen[name]:
            problem = record(name, run_id, env)
            if problem is None:
                break
            print(f"{name}: 跳过运行 {run_id}：{problem}")
        else:
            print(f"{name}: 没有能原样回放的运行，保留原有轨迹")
    return 0


if __name__ == "__main__":
    sys.exit(main())
