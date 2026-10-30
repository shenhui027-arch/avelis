"""分析智能体评测运行器（ADR 0009 第 5 点，最小版本）。

  uv run scripts/eval_analysis.py --repeat 2            串行跑完评测集（backend/tests/eval/analysis_questions.yaml）
  uv run scripts/eval_analysis.py --only d01,n01        只跑部分题

在演示环境上运行（合成电商数据集已灌入、元数据库已有演示口径），用 .env 中已授权用于验收的模型。
每题用题目指定的演示账号发起分析（wait=true），按 backend/tests/eval/scoring.py 判分；只有传输层错误（连接失败、SSL、
超时）时重跑一次。结果写到 .demo/eval/<时间>.json，并生成同名 Markdown 汇总（总体与分类的比例、重复运行的稳定性）。
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


def main() -> int:
    from tests.eval.scoring import load_questions, markdown, score, summarize, transport_error

    parser = argparse.ArgumentParser(description="分析智能体评测")
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--only", default="", help="逗号分隔的题号")
    args = parser.parse_args()

    env = demo.load_env()
    for key, value in env.items():  # 已设置的环境变量（如单独的元数据库）优先
        if key.startswith("AVELIS_"):
            os.environ.setdefault(key, value)
    data = load_questions()
    wanted = {x.strip() for x in args.only.split(",") if x.strip()}
    questions = [q for q in data["questions"] if not wanted or q["id"] in wanted]
    started = datetime.now()
    client = _client(env)
    password = env["AVELIS_DEMO_USER_PASSWORD"]
    headers: dict[str, dict[str, str]] = {}
    results, runs = [], []
    for attempt in range(1, args.repeat + 1):
        for q in questions:
            headers.setdefault(q["user"], _login(client, q["user"], password))
            began = time.monotonic()
            run = _ask(client, headers[q["user"]], q)
            retried = transport_error(run)
            if retried:
                run = _ask(client, headers[q["user"]], q)
            result = {**score(q, run), "repeat": attempt, "retried": retried, "seconds": round(time.monotonic() - began, 1),
                      "run_id": (run or {}).get("id"), "error": (run or {}).get("error"),
                      "tool_calls": (((run or {}).get("budget") or {}).get("used") or {}).get("tool_calls")}
            results.append(result)
            runs.append({"id": q["id"], "repeat": attempt, "run": run})
            print(f"[{attempt}/{args.repeat}] {q['id']} {q['category']}: {'通过' if result['passed'] else '未通过'}"
                  f"（{result['status']}，{result['seconds']}s）", flush=True)
    summary = summarize(results)
    meta = {"started": started.isoformat(timespec="seconds"), "version": data.get("version"), "repeat": args.repeat,
            "questions": [q["id"] for q in questions]}
    OUT.mkdir(parents=True, exist_ok=True)
    stem = OUT / started.strftime("%Y%m%d-%H%M%S")
    stem.with_suffix(".json").write_text(json.dumps({"meta": meta, "summary": summary, "results": results, "runs": runs},
                                                    ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    stem.with_suffix(".md").write_text(markdown(summary, results, meta), encoding="utf-8")
    print(f"总体通过 {summary['overall']['hits']}/{summary['overall']['runs']}；结果：{stem}.json、{stem}.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
