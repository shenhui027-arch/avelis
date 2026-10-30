"""E1：诊断性“补齐信息”实验（任务书第 12 节；manifest e1）。只在本实验进程里生效，产品默认路径不变。

  uv run --project backend python experiments/agent-v2/e1.py --plan          打印 48 次运行的顺序与预算上限（不调用模型）
  uv run --project backend python experiments/agent-v2/e1.py --run           执行（需要授权；调用真实模型、产生费用）
  uv run --project backend python experiments/agent-v2/e1.py --run --only h10 --conditions D0,D1 --repeat 1

费用账本默认上限 30 元、已花 0 元，每次开始前预留 1.5 元；可用 --cap、--spent、--reserve 调整。
预留依据：历史最大一次约 52 万输入、3.4 万输出，全部按高峰未命中计约 1.3 元。每次完成后保存费用与累计账本。
运行详情或用量不完整时，按已知费用加一次预留金额入账，并保留实测费用明细。

条件（材料见 e1_injections.yaml）：
- D0：共同基线 B0，正常运行；
- D1：理解阶段照常请求模型（请求次数与 D0 相同），之后用人工核对的范围替换模型给出的范围（不再待澄清）；
- D2：D1 + 在推理的上下文包末尾附上相关资产的定位与元数据（运行时以发起人身份 describe_asset，权限照常核对）；
- D3：D2 + 预检之后以发起人身份执行材料里的查询（query_metric / query_sql），结果是普通数据集，进入上下文包。
注入用到的元数据读取与查询不占用分析的工具与查询额度（执行前后恢复计数，注入另记），每次注入记一个 inject 步骤：
条件、注入的字符数（保守地按 1 字符 ≤ 1 token 作为 token 上界）、执行了哪些查询、被拒绝或失败的材料。

做法：在进程内替换 agent.understand、agent.precheck 与 AnalysisToolbox.context_pack，并重建流程图（图在编译时绑定
节点函数）；退出时恢复并再次重建。运行方式与 scripts/eval_analysis.py 相同（进程内 API，wait=true），判分用评分器 v2。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import random
import sys
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(Path(__file__).resolve().parent), str(ROOT / "scripts"),
                str(ROOT / "backend"), str(ROOT / "backend" / "src")]

from cost import Ledger, run_cost  # noqa: E402

MATERIAL = Path(__file__).with_name("e1_injections.yaml")
CONDITIONS = ("D0", "D1", "D2", "D3")
SEED = 20261031
COUNTERS = ("used_calls", "used_tools", "used_queries", "used_explore")


def load_material(path: Path = MATERIAL) -> dict[str, Any]:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))["scenarios"]


def plan(scenarios: list[str], conditions: list[str], repeat: int, seed: int = SEED) -> list[dict]:
    """运行顺序：每个重复内把“场景 × 条件”按固定种子打乱（任务书第 12 节：顺序随机化、每题每条件 2 次）。"""
    rng = random.Random(seed)
    out = []
    for r in range(1, repeat + 1):
        cells = [(s, c) for s in scenarios for c in conditions]
        rng.shuffle(cells)
        out += [{"scenario": s, "condition": c, "repeat": r} for s, c in cells]
    return out


def _record(toolbox, condition: str, what: str, **detail) -> None:
    toolbox._step("inject", what, {"condition": condition}, "ok" if not detail.get("failed") else "error",
                  {"condition": condition, **detail}, reason=f"实验注入（{condition}，不是产品行为）")


def _metadata_block(toolbox, assets: list[dict]) -> tuple[str, list[str]]:
    """D2：资产定位与正常元数据（列名、类型、注释），以发起人身份读取；读不到的表不注入，记下原因。"""
    blocks, failed = [], []
    for a in assets:
        detail = json.loads(toolbox.describe_asset(qualified=a["qualified"]))
        if detail.get("error"):
            failed.append(f"{a['qualified']}：{str(detail['error'])[:120]}")
            continue
        columns = [{"name": c.get("name"), "type": c.get("type"), "comment": (c.get("doc") or {}).get("comment")}
                   for c in detail.get("columns") or []][:40]
        blocks.append(json.dumps({"table": a["qualified"], "kind": detail.get("kind"), "comment": detail.get("comment"),
                                  "columns": columns}, ensure_ascii=False))
    text = ("实验提供的相关资产（定位与元数据，不含结论）：\n" + "\n".join(blocks)) if blocks else ""
    return text, failed


@contextlib.contextmanager
def install(condition: str, material: dict[str, Any], *, rebuild=None) -> Iterator[dict]:
    """在本进程内装上某个条件的注入；rebuild：重建流程图的函数（默认 service.reset_graph_cache）。"""
    from avelis.analysis import agent, service
    from avelis.analysis.tools import AnalysisToolbox, Scope

    if condition not in CONDITIONS:
        raise ValueError(f"未知条件 {condition}")
    rebuild = rebuild or service.reset_graph_cache
    stats: dict[str, Any] = {"condition": condition, "injected_chars": 0, "evidence": [], "failed": []}
    original = (agent.understand, agent.precheck, AnalysisToolbox.context_pack)
    level = CONDITIONS.index(condition)

    def understand(state, config):
        out = original[0](state, config)
        toolbox = config["configurable"]["toolbox"]
        scope = Scope.model_validate(material["scope"])
        toolbox.clarification = None  # 人工核对的范围已经明确：不停在待澄清
        toolbox.scope = scope
        toolbox.plan = {"scope": scope.model_dump(mode="json"), "problem": None, "injected": condition,
                        "model_scope": out.get("scope")}
        _record(toolbox, condition, "scope", scope=toolbox.plan["scope"], model_scope=out.get("scope"))
        return {**out, "scope": toolbox.plan["scope"], "phase": "precheck",
                "notes": [*(n for n in out.get("notes") or [] if not n.startswith("需要澄清")), f"实验注入范围（{condition}）"]}

    def precheck(state, config):
        out = original[1](state, config)
        toolbox = config["configurable"]["toolbox"]
        if out.get("phase") != "reason" or toolbox.stopped:
            return out
        saved = {k: getattr(toolbox.budget, k) for k in COUNTERS}
        refs = []
        for item in material.get("evidence") or []:
            call = {"query_metric": toolbox.query_metric, "query_sql": toolbox.query_sql}[item["tool"]]
            result = json.loads(call(**item["args"]))
            if result.get("dataset"):
                refs.append(result["dataset"])
            else:
                stats["failed"].append(f"{item['tool']}：{str(result.get('error') or result.get('message'))[:200]}")
        used = {k: getattr(toolbox.budget, k) - v for k, v in saved.items()}
        for k, v in saved.items():  # 注入不占用分析的额度
            setattr(toolbox.budget, k, v)
        toolbox.injected_refs = refs
        stats["evidence"] = refs
        _record(toolbox, condition, "evidence", datasets=refs, budget_used_by_injection=used, failed=stats["failed"])
        return out

    def context_pack(self):
        pack = original[2](self)
        if level < 2:
            return pack
        text, failed = _metadata_block(self, material.get("assets") or [])
        stats["failed"] += failed
        evidence_chars = sum(len(self._pack_block(r, self._datasets[r])) for r in getattr(self, "injected_refs", [])
                             if r in self._datasets)
        stats["injected_chars"] = len(text) + evidence_chars
        _record(self, condition, "metadata", chars=len(text), evidence_chars=evidence_chars,
                tokens_upper_bound=stats["injected_chars"], failed=failed)
        return pack + ("\n\n" + text if text else "")

    try:
        if level >= 1:
            agent.understand = understand
        if level >= 3:
            agent.precheck = precheck
        if level >= 2:
            AnalysisToolbox.context_pack = context_pack
        rebuild()
        yield stats
    finally:
        agent.understand, agent.precheck, AnalysisToolbox.context_pack = original
        rebuild()


def budget_bounds(runs: int) -> dict[str, int]:
    from avelis.analysis.tools import Budget

    b = Budget(queries=20)
    return {"runs": runs, "model_requests": runs * b.model_requests,
            "input_tokens": runs * b.input_tokens if b.input_tokens else "不强制（见 manifest b0.token_cap_decision）",
            "output_tokens": runs * b.output_tokens if b.output_tokens else "不强制（见 manifest b0.token_cap_decision）",
            "seconds_wall": runs * b.seconds}


def main() -> int:
    parser = argparse.ArgumentParser(description="E1 诊断性注入实验")
    parser.add_argument("--plan", action="store_true", help="只打印运行顺序与预算上限")
    parser.add_argument("--run", action="store_true", help="执行（调用真实模型，需要授权）")
    parser.add_argument("--only", default="", help="逗号分隔的场景")
    parser.add_argument("--conditions", default=",".join(CONDITIONS))
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--cap", default="30", help="累计费用上限（元）")
    parser.add_argument("--spent", default="0", help="此前已花费用（元）")
    parser.add_argument("--reserve", default="1.5", help="每次运行前预留费用（元）")
    args = parser.parse_args()
    try:
        ledger = Ledger(args.cap, args.spent, per_run_reserve_cny=args.reserve)
    except (InvalidOperation, ValueError):
        parser.error("--cap、--spent、--reserve 必须是非负有限金额")
    material = load_material()
    scenarios = [s for s in material if not args.only or s in args.only.split(",")]
    conditions = [c for c in args.conditions.split(",") if c]
    order = plan(scenarios, conditions, args.repeat)
    if len(order) > 48:
        raise SystemExit(f"超过 E1 冻结上限 48 次：{len(order)}")
    if not args.run:
        print(json.dumps({"order": order, "bounds": budget_bounds(len(order)), "ledger": ledger.view()},
                         ensure_ascii=False, indent=2))
        return 0

    import demo
    from eval_analysis import _ask, _client, _login, code_version
    from tests.eval import scoring_v2

    started = datetime.now()
    results = []
    client = None
    stop_reason = None
    out = demo.STATE / "experiments" / "e1"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{started.strftime('%Y%m%d-%H%M%S')}.json"
    meta = {"started": started.isoformat(timespec="seconds"), **code_version(),
            "scorer": scoring_v2.VERSION, "seed": SEED, "material": MATERIAL.name,
            "initial_spent_cny": str(args.spent)}

    def save_results():
        path.write_text(json.dumps({"meta": meta, "ledger": ledger.view(), "stop_reason": stop_reason,
                                    "results": results}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    for item in order:
        if not ledger.can_start():
            stop_reason = (f"费用预留不足：累计已花 {ledger.spent_cny} 元 + 每次预留 {ledger.per_run_reserve_cny} 元"
                           f" > 上限 {ledger.cap_cny} 元，停止后续运行")
            print(stop_reason, flush=True)
            break
        if client is None:
            env = demo.load_env()
            questions = {q["id"]: q for q in scoring_v2.load_questions()["questions"]}
            client = _client(env)
        q = questions[item["scenario"]]
        with install(item["condition"], material[item["scenario"]]) as stats:
            headers = _login(client, q["user"], env["AVELIS_DEMO_USER_PASSWORD"])
            began = time.monotonic()
            run = _ask(client, headers, q)  # 只有一次运行：真实网络故障不在这里重试（任务书：原始失败不能被覆盖）
        cost = run_cost(run or {})
        estimate_reasons = []
        if not run:
            estimate_reasons.append("未取得运行详情")
        else:
            if not run.get("id"):
                estimate_reasons.append("运行详情缺少 id")
            if not run.get("steps"):
                estimate_reasons.append("运行详情缺少步骤记录")
        if cost["missing_usage"] > 0:
            estimate_reasons.append(f"{cost['missing_usage']} 个模型步骤缺少用量")
        cost_estimated = bool(estimate_reasons)
        cost_estimate_reason = "；".join(estimate_reasons) or None
        cost_accounted = Decimal(cost["total_cny"]) + (ledger.per_run_reserve_cny if cost_estimated else Decimal("0"))
        ledger.add(cost_accounted)
        result = {**item, "seconds": round(time.monotonic() - began, 1), "run_id": (run or {}).get("id"),
                  "injection": stats, "score": scoring_v2.score(q, run), "run": run,
                  "cost": cost, "cost_estimated": cost_estimated, "cost_estimate_reason": cost_estimate_reason,
                  "cost_accounted_cny": str(cost_accounted), "ledger": ledger.view()}
        results.append(result)
        save_results()
        print(f"[{item['repeat']}] {item['scenario']} {item['condition']}: "
              f"{'通过' if result['score']['passed'] else '未通过'}（{(run or {}).get('status')}），"
              f"本次入账 {cost_accounted} 元，累计 {ledger.spent_cny}/{ledger.cap_cny} 元，"
              f"缺失用量步骤 {cost['missing_usage']}"
              + (f"，估算原因：{cost_estimate_reason}" if cost_estimated else ""), flush=True)
    save_results()
    print(f"结果：{path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
