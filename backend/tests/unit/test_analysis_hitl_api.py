"""HITL HTTP 契约：使用真实路由和服务、内存会话，不连接外部服务。"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from avelis.analysis import service
from avelis.api import analysis
from avelis.api.deps import current_user, get_session
from avelis.db import new_id, utcnow
from avelis.governance.errors import GovernanceError
from avelis.models import AnalysisRun
from tests.unit.test_analysis_hitl import waiting_run


@pytest.fixture
def api(monkeypatch):
    session, user, run, queued = waiting_run(monkeypatch)
    run.created_at, run.plan, run.report, run.verification = utcnow(), {}, {}, {}

    def flush():
        for obj in session.added:
            if getattr(obj, "id", None) is None:
                obj.id = new_id()
            if isinstance(obj, AnalysisRun):
                obj.created_at = obj.created_at or utcnow()
                obj.plan, obj.report, obj.verification = obj.plan or {}, obj.report or {}, obj.verification or {}
            session.store[(type(obj), obj.id)] = obj

    session.flush = flush
    monkeypatch.setattr(service, "warm_up", lambda: None)
    monkeypatch.setattr(service, "require_analysis_model", lambda s: SimpleNamespace(adapter="fake"))
    monkeypatch.setattr(service.access, "require_role", lambda *args: None)
    wakes = []
    monkeypatch.setattr(analysis.jobs, "_wake", lambda job_id: wakes.append(job_id))
    app = FastAPI()
    app.include_router(analysis.router)

    @app.exception_handler(GovernanceError)
    async def governance_error(_: Request, exc: GovernanceError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message, **exc.detail})

    app.dependency_overrides[current_user] = lambda: user
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as client:
        yield client, session, user, run, queued, wakes


def test_http_run_detail_and_resume_are_atomic_and_validate_all_decisions(api):
    client, _, _, run, queued, wakes = api
    path = f"/api/analysis/runs/{run.id}"
    pending = client.get(path)
    assert pending.status_code == 200
    assert pending.json()["proposal"]["items"][0]["id"] == "P1"
    assert pending.json()["proposal"]["items"][0]["source"] == "inferred"
    assert client.get(path + "/proposal").status_code == 404
    for decisions in ([], [{"item_id": "unknown", "decision": "confirmed"}],
                      [{"item_id": "P1", "decision": "confirmed"}] * 2):
        assert client.post(path + "/resume", json={"decisions": decisions}).status_code == 422
    assert queued == [] and wakes == [] and run.status == "waiting"
    body = {"decisions": [{"item_id": "P1", "decision": "rejected"}], "supplement": "按本次范围核对"}
    resumed = client.post(path + "/resume", json=body)
    assert resumed.status_code == 200, resumed.text
    payload = resumed.json()
    assert payload["status"] == "queued" and len(payload["experience_candidates"]) == 2
    assert payload["experience_candidates"][1]["kind"] == "negative"
    assert client.post(path + "/resume", json=body).status_code == 409
    assert len(queued) == len(wakes) == 1


def test_http_followup_inherits_user_text_and_accepts_the_documented_limit(api):
    client, _, _, parent, _, _ = api
    parent.status = "succeeded"
    scope = {"question_type": "other", "metric_codes": []}
    parent.checkpoint["confirmed_scope"] = scope
    text = "补" * 4000
    response = client.post(f"/api/analysis/runs/{parent.id}/followup", json={"text": text})
    assert response.status_code == 201, response.text
    child = response.json()
    assert child["parent_run_id"] == parent.id and child["status"] == "queued"
    assert child["experience_candidates"][0]["content"] == text
    assert child["experience_candidates"][0]["source"]["checkpoint"] == "conclusion"
    assert client.post(f"/api/analysis/runs/{parent.id}/followup", json={"text": text + "补"}).status_code == 422
    assert client.post(f"/api/analysis/runs/{parent.id}/followup", json={"text": "  "}).status_code == 422


@pytest.mark.parametrize("invalid", [
    {"kind": "instruction"},
    {"content": " "},
    {"content": "x" * 4001},
    {"refs": [{"object_type": "asset", "object_id": "a1", "version": True}]},
    {"refs": [{"object_type": "asset", "object_id": "x" * 65}]},
    {"source": {"run_id": "run", "checkpoint": " "}},
])
def test_http_experience_validates_input_at_schema_boundary(api, invalid):
    client, _, _, run, _, _ = api
    body = {"kind": "rule", "content": "本次范围规则", "refs": [],
            "source": {"run_id": run.id, "checkpoint": "assumptions"}, **invalid}
    assert client.post("/api/analysis/experiences", json=body).status_code == 422


def test_http_detail_serializes_confirmed_user_input_and_rejects_another_owner(api):
    client, _, user, run, _, _ = api
    run.status = "succeeded"
    run.checkpoint["confirmed_items"] = [{"id": "U:input", "kind": "hypothesis", "source": "user",
                                           "content": "用户补充", "status": "confirmed", "refs": []}]
    response = client.get(f"/api/analysis/runs/{run.id}")
    assert response.status_code == 200, response.text
    item = response.json()["confirmed_items"][0]
    assert item["source"] == "user" and item["status"] == "confirmed"
    user.id = "other-user"
    assert client.get(f"/api/analysis/runs/{run.id}").status_code == 404

