"""分析智能体评测运行器（ADR 0009 第 5 点，最小版本）。

  uv run --project backend python scripts/eval_analysis.py --repeat 2       串行跑完评测集（backend/tests/eval/analysis_questions.yaml）
  uv run --project backend python scripts/eval_analysis.py --repeat 2 --resume .demo/eval/<时间>.json
                                               续跑：沿用上次已有真实结果的运行，只重跑登录失效（401）与没跑到的
  uv run --project backend python scripts/eval_analysis.py --only d01,n01   只跑部分题
  uv run --project backend python scripts/eval_analysis.py --rescore .demo/eval/<时间>.json
                                               按当前题目定义对已保存的运行重新判分（不调用模型），写 <时间>-rescored.json / .md
（用后端的依赖环境运行：判分读 YAML、发起分析都需要后端依赖。）

在演示环境上运行（合成电商数据集已灌入、元数据库已有演示口径），用 .env 中已授权用于验收的模型。
每题用题目指定的演示账号发起分析（wait=true），按 backend/tests/eval/scoring.py 判分；只有传输层错误（连接失败、SSL、
超时）时重跑一次。每题开始前重新登录（访问令牌 30 分钟过期，整轮评测超过这个时长）。
结果写到 .demo/eval/<时间>.json，并生成同名 Markdown 汇总（总体与分类的比例、重复运行的稳定性）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "backend"), str(ROOT / "backend" / "src")]

import demo  # noqa: E402

OUT = demo.STATE / "eval"
WAIT_SECONDS = 600


def _client(env: dict[str, str]):
    from fastapi.testclient import TestClient

    from avelis.api.app import create_app
    from avelis.bootstrap import model_from_env

    model_from_env()  # 与真实模型验收相同：使用 .env 中的模型配置
    return TestClient(create_app())


def _login(client, username: str, password: str) -> dict[str, str]:
    resp = client.post("/api/auth/token", data={"username": username, "password": password})
    resp.raise_for_status()
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _ask(client, headers: dict[str, str], question: dict) -> dict | None:
    """发起一次分析并取回运行详情；发不出去或接口报错时返回 None（判分记为没完成）。"""
    try:
        resp = client.post("/api/analysis/runs", params={"wait": "true"}, headers=headers, timeout=WAIT_SECONDS,
                           json={"question": question["question"], "window": question["window"]})
    except Exception as exc:  # noqa: BLE001 - 传输层错误
        return {"status": "failed", "error": f"ConnectionError: {exc}"}
    if resp.status_code >= 400:
        return {"status": "failed", "error": f"HTTP {resp.status_code}: {resp.text[:500]}"}
    return resp.json()


def reusable(saved: dict) -> dict[tuple[str, int], tuple[dict, dict]]:
    """上次结果里可以沿用的运行：系统里确有这次分析（有运行编号，即调用过模型），状态是 succeeded / failed / paused。
    登录失效（401）、接口报错没建出运行的，以及没跑到的，都不在这里（续跑时重跑）。"""
    results = {(r["id"], r["repeat"]): r for r in saved["results"]}
    out = {}
    for item in saved["runs"]:
        run, key = item.get("run") or {}, (item["id"], item["repeat"])
        if run.get("id") and run.get("status") in ("succeeded", "failed", "paused") and key in results:
            out[key] = (results[key], item)
    return out


def run_questions(client, questions: list[dict], repeat: int, password: str,
                  reuse: dict | None = None) -> tuple[list[dict], list[dict]]:
    from tests.eval.scoring import score, transport_error

    results, runs = [], []
    for attempt in range(1, repeat + 1):
        for q in questions:
            if (q["id"], attempt) in (reuse or {}):  # 续跑：沿用上次的真实结果
                result, item = reuse[(q["id"], attempt)]
                results.append({**result, "resumed": False})
                runs.append(item)
                continue
            headers = _login(client, q["user"], password)  # 每题取新令牌：访问令牌 30 分钟过期
            began = time.monotonic()
            run = _ask(client, headers, q)
            retried = transport_error(run)
            if retried:
                run = _ask(client, headers, q)
            result = {**score(q, run), "repeat": attempt, "retried": retried, "seconds": round(time.monotonic() - began, 1),
                      "run_id": (run or {}).get("id"), "error": (run or {}).get("error"),
                      "tool_calls": (((run or {}).get("budget") or {}).get("used") or {}).get("tool_calls"),
                      **({"resumed": True} if reuse is not None else {})}
            results.append(result)
            runs.append({"id": q["id"], "repeat": attempt, "run": run})
            print(f"[{attempt}/{repeat}] {q['id']} {q['category']}: {'通过' if result['passed'] else '未通过'}"
                  f"（{result['status']}，{result['seconds']}s）" + ("，续跑" if reuse is not None else ""), flush=True)
    return results, runs


def main() -> int:
    from tests.eval.scoring import load_questions, markdown, summarize

    parser = argparse.ArgumentParser(description="分析智能体评测")
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--resume", type=Path, help="上次的评测结果：沿用已有真实结果的运行，重跑其余的")
    parser.add_argument("--only", default="", help="逗号分隔的题号")
    parser.add_argument("--rescore", type=Path, help="已保存的评测结果：按当前题目定义重新判分")
    args = parser.parse_args()
    if args.rescore:
        return rescore(args.rescore)

    env = demo.load_env()
    for key, value in env.items():  # 已设置的环境变量（如单独的元数据库）优先
        if key.startswith("AVELIS_"):
            os.environ.setdefault(key, value)
    data = load_questions()
    wanted = {x.strip() for x in args.only.split(",") if x.strip()}
    questions = [q for q in data["questions"] if not wanted or q["id"] in wanted]
    started = datetime.now()
    reuse = reusable(json.loads(args.resume.read_text(encoding="utf-8"))) if args.resume else {}
    results, runs = run_questions(_client(env), questions, args.repeat, env["AVELIS_DEMO_USER_PASSWORD"], reuse)
    summary = summarize(results)
    meta = {"started": started.isoformat(timespec="seconds"), "version": data.get("version"), "repeat": args.repeat,
            "questions": [q["id"] for q in questions]}
    if args.resume:
        meta.update(resumed_from=args.resume.name, reused=sum(1 for r in results if not r.get("resumed")),
                    resumed=sum(1 for r in results if r.get("resumed")))
    OUT.mkdir(parents=True, exist_ok=True)
    stem = OUT / started.strftime("%Y%m%d-%H%M%S")
    stem.with_suffix(".json").write_text(json.dumps({"meta": meta, "summary": summary, "results": results, "runs": runs},
                                                    ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    stem.with_suffix(".md").write_text(markdown(summary, results, meta), encoding="utf-8")
    print(f"总体通过 {summary['overall']['hits']}/{summary['overall']['runs']}；结果：{stem}.json、{stem}.md")
    return 0


def rescore(path: Path) -> int:
    from tests.eval.scoring import load_questions, markdown, score, summarize

    saved = json.loads(path.read_text(encoding="utf-8"))
    questions = {q["id"]: q for q in load_questions()["questions"]}
    previous = {(r["id"], r["repeat"]): r for r in saved["results"]}
    results = []
    for item in saved["runs"]:
        old = previous[(item["id"], item["repeat"])]
        results.append({**score(questions[item["id"]], item["run"]), "repeat": item["repeat"], "retried": old["retried"],
                        "seconds": old["seconds"], "run_id": old["run_id"], "error": old["error"],
                        "tool_calls": old["tool_calls"]})
    summary = summarize(results)
    meta = {**saved["meta"], "rescored_from": path.name}
    stem = path.with_name(path.stem + "-rescored")
    stem.with_suffix(".json").write_text(json.dumps({"meta": meta, "summary": summary, "results": results},
                                                    ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    stem.with_suffix(".md").write_text(markdown(summary, results, meta), encoding="utf-8")
    print(f"重新判分：总体通过 {summary['overall']['hits']}/{summary['overall']['runs']}；结果：{stem}.json、{stem}.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
