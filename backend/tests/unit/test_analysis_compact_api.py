"""手动压缩请求复用运行交互权限，在恢复前保持请求标志。"""

import pytest

from avelis.analysis import service
from avelis.models import AuditEvent
from tests.unit.test_analysis_live import live_api  # noqa: F401


@pytest.mark.parametrize("status", ["queued", "running", "waiting"])
def test_compact_accepts_live_run_and_only_records_request(live_api, status):
    client, session, user, run, queued = live_api
    run.status = status
    checkpoint = dict(run.checkpoint)
    response = client.post(f"/api/analysis/runs/{run.id}/compact")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == status
    assert run.compact_requested is True
    assert run.checkpoint == checkpoint and queued == []
    assert any(isinstance(event, AuditEvent) and event.action == "analysis.compact"
               and event.actor_id == user.id and event.object_id == run.id for event in session.added)


@pytest.mark.parametrize("is_admin", [False, True])
def test_compact_rejects_another_user_even_when_admin(live_api, monkeypatch, is_admin):
    client, session, user, run, _ = live_api
    user.id = "another-user"
    monkeypatch.setattr(service.access, "is_admin", lambda *args: is_admin)
    response = client.post(f"/api/analysis/runs/{run.id}/compact")
    assert response.status_code == 404, response.text
    assert not run.compact_requested
    assert not any(isinstance(event, AuditEvent) for event in session.added)


@pytest.mark.parametrize("status", ["succeeded", "failed", "stopped", "clarify"])
def test_compact_rejects_finished_runs(live_api, status):
    client, _, _, run, _ = live_api
    run.status = status
    response = client.post(f"/api/analysis/runs/{run.id}/compact")
    assert response.status_code == 409, response.text
    assert run.status == status and not run.compact_requested


def test_compact_rejects_missing_run(live_api):
    client, _, _, _, _ = live_api
    assert client.post("/api/analysis/runs/missing/compact").status_code == 404


def test_waiting_compact_request_survives_resume(live_api, monkeypatch):
    from avelis.api import analysis

    client, _, _, run, queued = live_api
    monkeypatch.setattr(analysis.jobs, "_wake", lambda job_id: None)
    path = f"/api/analysis/runs/{run.id}"
    assert client.post(path + "/compact").status_code == 200
    assert run.status == "waiting" and run.compact_requested is True and queued == []
    response = client.post(path + "/resume", json={"decisions": [{"item_id": "P1", "decision": "rejected"}]})
    assert response.status_code == 200, response.text
    assert run.status == "queued" and run.compact_requested is True and len(queued) == 1
