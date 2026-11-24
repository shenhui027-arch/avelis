"""M3B Ask & Analysis SSE 流式推送、断线重连与权限投影测试。"""

from __future__ import annotations

import json
import threading
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import JSON, MetaData, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from avelis import cache
from avelis.analysis import live
from avelis.analysis import service as analysis_service
from avelis.api import analysis as analysis_api
from avelis.api import ask as ask_api
from avelis.api.deps import current_user, get_session
from avelis.api.sse import SSE_HEADERS, format_sse
from avelis.ask import agent as ask_agent
from avelis.db import utcnow
from avelis.governance.errors import GovernanceError
from avelis.models import (
    AnalysisDataset,
    AnalysisMessage,
    AnalysisRun,
    AnalysisStep,
    AnalysisTask,
    Answer,
    AppSetting,
    AskThread,
    Job,
    LlmPricing,
    LlmQuota,
    LlmUsageLedger,
    ModelAssignment,
    ModelProvider,
    User,
    UserRole,
)


def _parse_sse(raw: str) -> list[dict]:
    events = []
    for block in raw.strip().split("\n\n"):
        if not block.strip():
            continue
        lines = block.splitlines()
        if all(line.startswith(":") for line in lines):
            continue
        ev: dict = {"event": "message", "data": None, "id": None}
        data_lines = []
        for line in lines:
            if line.startswith("event:"):
                ev["event"] = line[6:].strip()
            elif line.startswith("id:"):
                ev["id"] = line[3:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        raw_data = "\n".join(data_lines)
        try:
            ev["data"] = json.loads(raw_data)
        except ValueError:
            ev["data"] = raw_data
        events.append(ev)
    return events


def test_sse_frame_formatting():
    frame = format_sse("delta", {"delta": "hello\nworld"}, event_id=7)
    assert "id: 7\nevent: delta\ndata: " in frame
    assert SSE_HEADERS["Cache-Control"] == "no-cache, no-transform"
    assert SSE_HEADERS["X-Accel-Buffering"] == "no"


def _setup_sqlite_app():
    cache.clear_local_fallbacks()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    meta = MetaData()
    for model in (
        User,
        UserRole,
        ModelProvider,
        ModelAssignment,
        AppSetting,
        AskThread,
        Answer,
        AnalysisTask,
        AnalysisRun,
        AnalysisStep,
        AnalysisDataset,
        AnalysisMessage,
        Job,
        LlmUsageLedger,
        LlmPricing,
        LlmQuota,
    ):
        table = model.__table__.to_metadata(meta)
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()
    meta.create_all(engine)
    with Session(engine) as s:
        u = User(id="u1", username="analyst", display_name="Analyst", password_hash="x", is_active=True)
        s.add_all([u, UserRole(user_id="u1", role="analyst")])
        s.commit()

    app = FastAPI()
    app.include_router(ask_api.router)
    app.include_router(analysis_api.router)

    @app.exception_handler(GovernanceError)
    async def _gov(_: Request, exc: GovernanceError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message, **exc.detail})

    def _session():
        with Session(engine) as s:
            yield s
            s.commit()

    def _user():
        with Session(engine) as s:
            return s.get(User, "u1")

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[current_user] = _user
    return engine, app


def test_ask_sse_stream_and_json_fallback(monkeypatch):
    engine, app = _setup_sqlite_app()
    try:

        def fake_run_turn(session, user, question, thread_id, *, on_event=None):
            if on_event is not None:
                on_event("status", {"phase": "understand", "status": "running"})
                on_event("thinking", {"delta": "思考", "content": "思考", "phase": "explain"})
                on_event("delta", {"delta": "结论", "content": "结论", "phase": "explain"})
            t = AskThread(id="t1", user_id=user.id, title=question, status="active")
            ans = Answer(
                id="a1",
                thread_id="t1",
                question=question,
                asked_by=user.id,
                asked_at=utcnow(),
                status="answered",
                model_adapter="demo",
                intent={},
                plan={},
                metric_version_ids=[],
                evidence={},
                result={},
                freshness={},
                cache_hit=False,
                steps=[{"node": "explain", "source": "model"}],
                explanation="结论",
                explanation_source="model",
            )
            session.merge(t)
            session.merge(ans)
            session.flush()
            return ans

        monkeypatch.setattr(ask_agent, "run_turn", fake_run_turn)
        client = TestClient(app)

        # Standard JSON POST /api/ask remains backward compatible
        r_json = client.post("/api/ask", json={"question": "昨日指标"})
        assert r_json.status_code == 200 and r_json.json()["id"] == "a1"

        # POST /api/ask with Accept: text/event-stream streams SSE
        r_accept = client.post(
            "/api/ask",
            json={"question": "昨日指标"},
            headers={"Accept": "text/event-stream"},
        )
        assert r_accept.status_code == 200
        assert r_accept.headers["content-type"].startswith("text/event-stream")
        evs = _parse_sse(r_accept.text)
        assert [e["event"] for e in evs] == ["status", "thinking", "delta", "done"]
        assert evs[-1]["data"]["explanation"] == "结论"

        # POST /api/ask/stream also streams SSE
        r_stream = client.post("/api/ask/stream", json={"question": "昨日指标"})
        assert r_stream.status_code == 200
        assert [e["event"] for e in _parse_sse(r_stream.text)] == ["status", "thinking", "delta", "done"]
    finally:
        engine.dispose()
        cache.clear_local_fallbacks()


def test_analysis_sse_terminal_active_and_restricted_projection(monkeypatch):
    engine, app = _setup_sqlite_app()
    try:
        with Session(engine) as s:
            s.add(
                AnalysisRun(
                    id="run-term",
                    user_id="u1",
                    question="完成分析",
                    trigger="manual",
                    status="succeeded",
                    window={},
                    plan={},
                    report={"title": "报告"},
                    verification={"ok": True},
                    budget={},
                )
            )
            s.add_all(
                [
                    AnalysisStep(id="s1", run_id="run-term", seq=1, kind="plan", args={}, status="ok", output={}),
                    AnalysisStep(
                        id="s2", run_id="run-term", seq=2, kind="model", args={}, status="ok", output={"text": "ok"}
                    ),
                ]
            )
            s.add(
                AnalysisRun(
                    id="run-live",
                    user_id="u1",
                    question="运行中分析",
                    trigger="manual",
                    status="running",
                    window={},
                    plan={},
                    report={},
                    verification={},
                    budget={},
                )
            )
            s.commit()

        client = TestClient(app)

        # 404 when run does not exist
        assert client.get("/api/analysis/runs/missing/stream").status_code == 404

        # Terminal run emits steps > after_seq and done immediately
        r_term = client.get("/api/analysis/runs/run-term/stream?after_seq=1")
        assert r_term.status_code == 200
        evs = _parse_sse(r_term.text)
        assert [e["event"] for e in evs] == ["step", "status", "done"]
        assert evs[0]["data"]["step"]["seq"] == 2
        assert evs[-1]["data"]["status"] == "succeeded"

        # Active run streams live thinking/delta (unless restricted) and finishes on terminal DB update
        live_snap = {
            "phase": "model",
            "after_seq": 0,
            "started_at": utcnow().isoformat(),
            "thinking": "推理片段",
            "text": "正文片段",
        }
        monkeypatch.setattr(live, "read", lambda rid: live_snap if rid == "run-live" else None)

        def finish_run_soon(step_id: str, seq: int):
            time.sleep(0.15)
            with Session(engine) as s:
                r = s.get(AnalysisRun, "run-live")
                r.status = "succeeded"
                r.report = {"title": "完成"}
                s.add(
                    AnalysisStep(
                        id=step_id,
                        run_id="run-live",
                        seq=seq,
                        kind="model",
                        args={},
                        status="ok",
                        output={"text": "done"},
                    )
                )
                s.commit()
            live.notify("run-live")

        t = threading.Thread(target=finish_run_soon, args=("s-live-1", 1), daemon=True)
        t.start()
        r_live = client.get("/api/analysis/runs/run-live/stream")
        t.join(timeout=3)
        assert r_live.status_code == 200
        live_evs = _parse_sse(r_live.text)
        names = [e["event"] for e in live_evs]
        assert "thinking" in names and "delta" in names and "step" in names and names[-1] == "done"

        # When live.view projects restricted=True, thinking and delta must NOT be emitted
        with Session(engine) as s:
            r = s.get(AnalysisRun, "run-live")
            r.status = "running"
            s.commit()

        real_view = analysis_service.run_view

        def restricted_run_view(session, run, *, full=True, user=None):
            data = real_view(session, run, full=full, user=user)
            if data.get("live"):
                data["live"] = {**data["live"], "restricted": True, "thinking": None, "text": None}
            return data

        monkeypatch.setattr(analysis_service, "run_view", restricted_run_view)
        t2 = threading.Thread(target=finish_run_soon, args=("s-live-2", 2), daemon=True)
        t2.start()
        r_rest = client.get("/api/analysis/runs/run-live/stream?after_seq=1")
        t2.join(timeout=3)
        rest_names = [e["event"] for e in _parse_sse(r_rest.text)]
        assert "thinking" not in rest_names and "delta" not in rest_names and rest_names[-1] == "done"
    finally:
        engine.dispose()
        cache.clear_local_fallbacks()
