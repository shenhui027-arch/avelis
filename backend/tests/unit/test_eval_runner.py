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
        return Response(
            200, {"id": f"run{len(self.asked)}", "status": "succeeded", "report": {"causes": []}, "verification": {}}
        )


def test_every_question_logs_in_again_so_an_expired_token_is_never_reused(capsys):
    client = ExpiringClient()
    results, runs = eval_analysis.run_questions(client, QUESTIONS, 2, "pw")
    assert client.issued == 4 and len(client.asked) == 4  # 2 题 × 2 次，每题一个新令牌
    assert all(r["run"]["status"] == "succeeded" and "401" not in str(r["run"]) for r in runs)
    assert all(r["error"] is None for r in results)


def test_resume_reuses_real_runs_and_reruns_401_and_missing(capsys):
    first = {
        "results": [
            {"id": "d01", "repeat": 1, "passed": True, "status": "succeeded"},
            {"id": "n01", "repeat": 1, "passed": False, "status": "failed"},
        ],
        "runs": [
            {"id": "d01", "repeat": 1, "run": {"id": "old1", "status": "succeeded"}},
            {"id": "n01", "repeat": 1, "run": {"status": "failed", "error": "HTTP 401: 令牌已过期"}},
        ],
    }
    reuse = eval_analysis.reusable(first)
    assert set(reuse) == {("d01", 1)}  # 401 没建出运行：不沿用
    client = ExpiringClient()
    results, runs = eval_analysis.run_questions(client, QUESTIONS, 2, "pw", reuse)
    assert [(r["id"], r["repeat"], r["resumed"]) for r in results] == [
        ("d01", 1, False),
        ("n01", 1, True),
        ("d01", 2, True),
        ("n01", 2, True),
    ]
    assert runs[0]["run"]["id"] == "old1" and len(client.asked) == 3 and client.issued == 3


def test_confirm_all_executes_resumed_job_without_a_worker(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import JSON, MetaData, create_engine, select
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from avelis import db, jobs
    from avelis.analysis import service
    from avelis.api import analysis, deps
    from avelis.models import AnalysisDataset, AnalysisMessage, AnalysisRun, AnalysisStep, AnalysisTask, Job, User

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in (User, AnalysisTask, AnalysisRun, AnalysisStep, AnalysisDataset, AnalysisMessage, Job):
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "session_factory", lambda: factory)
    monkeypatch.setattr(deps, "session_factory", lambda: factory)
    monkeypatch.setattr(service.audit, "record", lambda *args, **kwargs: None)
    wakes, executed = [], []
    monkeypatch.setattr(jobs, "_wake", lambda job_id: wakes.append(job_id))

    def execute(session, run_id, job_id, attempt):
        job, run = session.get(Job, job_id), session.get(AnalysisRun, run_id)
        assert job.status == "running" and job.attempts == attempt == 1
        assert run.checkpoint["feedback"]["decisions"] == [{"item_id": "P1", "decision": "confirmed"}]
        run.status, run.finished_at = "succeeded", db.utcnow()
        job.status, job.finished_at = "succeeded", db.utcnow()
        executed.append(job_id)

    monkeypatch.setattr(service, "execute", execute)

    def no_wait(seconds):
        raise AssertionError("确认后应在本进程执行，不应等待 worker")

    monkeypatch.setattr(eval_analysis.time, "sleep", no_wait)
    with factory.begin() as session:
        user = User(id="eval-user", username="eval", display_name="评测用户", password_hash="unused")
        run = AnalysisRun(
            id="eval-run",
            user_id=user.id,
            question="检查收入变化",
            status="waiting",
            trigger="manual",
            checkpoint={
                "pending": {"kind": "assumptions"},
                "proposal": {
                    "items": [{"id": "P1", "kind": "hypothesis", "content": "检查收入变化", "source": "inferred"}]
                },
            },
        )
        session.add_all([user, run])
    app = FastAPI()
    app.include_router(analysis.router)
    app.dependency_overrides[deps.current_user] = lambda: user
    path = f"/api/analysis/runs/{run.id}"
    with TestClient(app) as client:
        result = eval_analysis._confirm_all(client, {}, client.get(path).json())
        assert result["status"] == "succeeded" and result["id"] == run.id
    with factory() as session:
        job = session.scalar(select(Job))
        assert job.status == "succeeded" and job.attempts == 1
        assert executed == wakes == [job.id]
    engine.dispose()
