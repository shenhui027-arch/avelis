"""评测运行器（scripts/eval_analysis.py）：每题重新登录、续跑只重跑没有真实结果的运行。不调用模型。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

import eval_analysis  # noqa: E402

from tests.eval.scoring import load_questions  # noqa: E402

QUESTIONS = [q for q in load_questions()["questions"] if q["id"] in ("d01", "n01")]


class Response:
    def __init__(self, status: int, body: dict):
        self.status_code, self.body, self.text = status, body, str(body)

    def json(self):
        return self.body

    def raise_for_status(self):
        assert self.status_code < 400, self.status_code


class ExpiringClient:
    """每个令牌只在签发后的第一次分析请求里有效（模拟 30 分钟过期）；过期令牌返回 401。"""

    def __init__(self):
        self.issued, self.used, self.asked = 0, set(), []

    def post(self, path, data=None, json=None, headers=None, params=None, timeout=None):
        if path == "/api/auth/token":
            self.issued += 1
            return Response(200, {"access_token": f"t{self.issued}"})
        token = headers["Authorization"].split()[-1]
        if token in self.used:
            return Response(401, {"detail": "令牌已过期"})
        self.used.add(token)
        self.asked.append(json["question"])
        return Response(200, {"id": f"run{len(self.asked)}", "status": "succeeded", "report": {"causes": []},
                              "verification": {}})


def test_every_question_logs_in_again_so_an_expired_token_is_never_reused(capsys):
    client = ExpiringClient()
    results, runs = eval_analysis.run_questions(client, QUESTIONS, 2, "pw")
    assert client.issued == 4 and len(client.asked) == 4  # 2 题 × 2 次，每题一个新令牌
    assert all(r["run"]["status"] == "succeeded" and "401" not in str(r["run"]) for r in runs)
    assert all(r["error"] is None for r in results)


def test_resume_reuses_real_runs_and_reruns_401_and_missing(capsys):
    first = {"results": [{"id": "d01", "repeat": 1, "passed": True, "status": "succeeded"},
                         {"id": "n01", "repeat": 1, "passed": False, "status": "failed"}],
             "runs": [{"id": "d01", "repeat": 1, "run": {"id": "old1", "status": "succeeded"}},
                      {"id": "n01", "repeat": 1, "run": {"status": "failed", "error": "HTTP 401: 令牌已过期"}}]}
    reuse = eval_analysis.reusable(first)
    assert set(reuse) == {("d01", 1)}  # 401 没建出运行：不沿用
    client = ExpiringClient()
    results, runs = eval_analysis.run_questions(client, QUESTIONS, 2, "pw", reuse)
    assert [(r["id"], r["repeat"], r["resumed"]) for r in results] == [
        ("d01", 1, False), ("n01", 1, True), ("d01", 2, True), ("n01", 2, True)]
    assert runs[0]["run"]["id"] == "old1" and len(client.asked) == 3 and client.issued == 3
