"""E2：只比较产品的 R / H 开关，不装入 E1 的注入材料。

  uv run --project backend python experiments/agent-v2/e2.py --plan
  uv run --project backend python experiments/agent-v2/e2.py --run --truth <真值 JSON 路径>
  uv run --project backend python experiments/agent-v2/e2.py --plan --only h10 --conditions B0,R --repeat 1

默认按固定种子运行 16 个场景 × 4 个条件 × 2 次重复，最多 128 次。
费用上限默认 20 元，开始前预留 1.5 元；用量不完整时按已知费用加一次预留入账。
运行器不主动加载配置文件；登录口令由进程环境变量 AVELIS_DEMO_USER_PASSWORD 提供。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(Path(__file__).resolve().parent), str(ROOT / "scripts"),
                str(ROOT / "backend"), str(ROOT / "backend" / "src")]

from cost import Ledger, run_cost  # noqa: E402
from e1 import budget_bounds, plan as _plan  # noqa: E402

SCENARIOS = ("h10", "e01", "h07", "f02", "h09", "d02", "e02", "h05",
             "d06", "f01", "c02", "g01", "d03", "n01", "a02", "h12")
CONDITIONS = ("B0", "R", "H", "RH")
SEED = 20261101
MAX_RUNS = 128
FLAGS = {
    "B0": {"analysis_retrieval": False, "analysis_investigation": False},
    "R": {"analysis_retrieval": True, "analysis_investigation": False},
    "H": {"analysis_retrieval": False, "analysis_investigation": True},
    "RH": {"analysis_retrieval": True, "analysis_investigation": True},
}
ENV_FLAGS = {"analysis_retrieval": "AVELIS_ANALYSIS_RETRIEVAL",
             "analysis_investigation": "AVELIS_ANALYSIS_INVESTIGATION"}


def plan(scenarios: list[str], conditions: list[str], repeat: int = 2, seed: int = SEED) -> list[dict]:
    """每个重复内打乱完整的“场景 × 条件”；在生成顺序前检查冻结上限。"""
    for label, selected, allowed in (("场景", scenarios, SCENARIOS), ("条件", conditions, CONDITIONS)):
        if not selected or len(selected) != len(set(selected)):
            raise ValueError(f"{label}不能为空或重复")
        unknown = set(selected) - set(allowed)
        if unknown:
            raise ValueError(f"未知{label}：{', '.join(sorted(unknown))}")
    if repeat < 1:
        raise ValueError("--repeat 必须是正整数")
    runs = len(scenarios) * len(conditions) * repeat
    if runs > MAX_RUNS:
        raise ValueError(f"超过 E2 冻结上限 {MAX_RUNS} 次：{runs}")
    return _plan(scenarios, conditions, repeat, seed)


def assert_flags(condition: str) -> dict[str, bool]:
    """核对实际产品配置，失配时立即停止，不能继续记为该实验条件。"""
    from avelis.config import get_settings

    if condition not in FLAGS:
        raise ValueError(f"未知条件 {condition}")
    settings = get_settings()
    actual = {field: getattr(settings, field) for field in ENV_FLAGS}
    if actual != FLAGS[condition]:
        raise RuntimeError(f"条件 {condition} 的产品开关不匹配：期望 {FLAGS[condition]}，实际 {actual}")
    return actual


@contextlib.contextmanager
def install(condition: str) -> Iterator[dict[str, bool]]:
    """只切换进程内的产品开关，退出（含异常）后恢复原值并清理配置缓存。"""
    from avelis.config import get_settings

    if condition not in FLAGS:
        raise ValueError(f"未知条件 {condition}")
    original = {name: os.environ.get(name) for name in ENV_FLAGS.values()}
    try:
        for field, name in ENV_FLAGS.items():
            os.environ[name] = str(FLAGS[condition][field]).lower()
        get_settings.cache_clear()
        # 开关在运行时读取，图编译时未绑定，因此无需重建图缓存。
        yield assert_flags(condition)
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        get_settings.cache_clear()


def main() -> int:
    parser = argparse.ArgumentParser(description="E2 产品开关 2×2 实验")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="只打印运行顺序与预算上限，不读取真值")
    mode.add_argument("--run", action="store_true", help="执行（调用真实模型，产生费用）")
    parser.add_argument("--truth", type=Path, help="显式提供评分真值 JSON；仅执行时必填")
    parser.add_argument("--only", default="", help="逗号分隔的冻结场景")
    parser.add_argument("--conditions", default=",".join(CONDITIONS))
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--cap", default="20", help="累计费用上限（元）")
    parser.add_argument("--spent", default="0", help="此前已花费用（元）")
    parser.add_argument("--reserve", default="1.5", help="每次运行前预留费用（元）")
    args = parser.parse_args()
    scenarios = ([s.strip() for s in args.only.split(",") if s.strip()]
                 if args.only else list(SCENARIOS))
    conditions = [c.strip() for c in args.conditions.split(",") if c.strip()]
    try:
        ledger = Ledger(args.cap, args.spent, per_run_reserve_cny=args.reserve)
    except (InvalidOperation, ValueError):
        parser.error("--cap、--spent、--reserve 必须是非负有限金额")
    try:
        order = plan(scenarios, conditions, args.repeat)
    except ValueError as exc:
        parser.error(str(exc))
    if not args.run:
        print(json.dumps({"order": order, "bounds": budget_bounds(len(order)), "ledger": ledger.view()},
                         ensure_ascii=False, indent=2))
        return 0
    if args.truth is None:
        parser.error("--run 必须显式提供 --truth，不能默认读取演示真值")

    from tests.eval import scoring_v2_1_1

    truth = json.loads(args.truth.read_text(encoding="utf-8"))
    if not isinstance(truth, dict) or not truth:
        parser.error("--truth 必须是非空 JSON 对象")
    questions = {q["id"]: q for q in scoring_v2_1_1.load_questions(truth)["questions"]}
    missing = set(scenarios) - questions.keys()
    if missing:
        parser.error(f"题目文件缺少场景：{', '.join(sorted(missing))}")

    import demo
    from eval_analysis import _ask, _client, _login, code_version

    started = datetime.now()
    results = []
    client = None
    stop_reason = None
    out = demo.STATE / "experiments" / "e2"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{started.strftime('%Y%m%d-%H%M%S')}.json"
    meta = {"started": started.isoformat(timespec="seconds"), **code_version(),
            "conditions": conditions, "flags": {c: FLAGS[c].copy() for c in conditions},
            "scenarios": scenarios, "repeat": args.repeat, "seed": SEED,
            "scorer": scoring_v2_1_1.VERSION,
            "scorer_sha256": hashlib.sha256(Path(scoring_v2_1_1.__file__).read_bytes()).hexdigest(),
            "truth": str(args.truth), "initial_spent_cny": str(args.spent)}

    def save_results():
        path.write_text(json.dumps({"meta": meta, "ledger": ledger.view(), "stop_reason": stop_reason,
                                    "results": results}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    try:
        for item in order:
            if not ledger.can_start():
                stop_reason = (f"费用预留不足：累计已花 {ledger.spent_cny} 元 + 每次预留 {ledger.per_run_reserve_cny} 元"
                               f" > 上限 {ledger.cap_cny} 元，停止后续运行")
                print(stop_reason, flush=True)
                break
            q = questions[item["scenario"]]
            with install(item["condition"]):
                if client is None:
                    if not os.environ.get("AVELIS_DEMO_USER_PASSWORD"):
                        raise RuntimeError("缺少进程环境变量 AVELIS_DEMO_USER_PASSWORD")
                    client = _client(dict(os.environ))
                headers = _login(client, q["user"], os.environ["AVELIS_DEMO_USER_PASSWORD"])
                flags = assert_flags(item["condition"])  # 发起每次运行前再次核对，失配立即停止。
                began = time.monotonic()
                run = _ask(client, headers, q)  # 只调用一次，保留原始失败，运行器不重试。
                seconds = round(time.monotonic() - began, 1)
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
            cost_accounted = (Decimal(cost["total_cny"])
                              + (ledger.per_run_reserve_cny if cost_estimated else Decimal("0")))
            ledger.add(cost_accounted)
            result = {**item, "flags": flags, "seconds": seconds, "run_id": (run or {}).get("id"),
                      "score": scoring_v2_1_1.score(q, run), "run": run,
                      "cost": cost, "cost_estimated": cost_estimated, "cost_estimate_reason": cost_estimate_reason,
                      "cost_accounted_cny": str(cost_accounted), "ledger": ledger.view()}
            results.append(result)
            save_results()
            print(f"[{item['repeat']}] {item['scenario']} {item['condition']}: "
                  f"{'通过' if result['score']['passed'] else '未通过'}（{(run or {}).get('status')}），"
                  f"本次入账 {cost_accounted} 元，累计 {ledger.spent_cny}/{ledger.cap_cny} 元，"
                  f"缺失用量步骤 {cost['missing_usage']}"
                  + (f"，估算原因：{cost_estimate_reason}" if cost_estimated else ""), flush=True)
    except Exception as exc:
        stop_reason = f"运行中止：{type(exc).__name__}: {exc}"
        raise
    finally:
        save_results()
    print(f"结果：{path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
