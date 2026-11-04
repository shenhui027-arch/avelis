"""对话列表、逐轮详情与显示名的 HTTP 合约；复用离线数据库。"""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from avelis.analysis import service
from avelis.api.deps import current_user
from avelis.models import AnalysisRun, AnalysisTask, AuditEvent, User, UserRole
from tests.unit.test_analysis_dataset_api import api  # noqa: F401  pytest 夹具按参数名注入


@pytest.fixture
def model(monkeypatch):
    monkeypatch.setattr(service, "require_analysis_model", lambda *a, **kw: SimpleNamespace(adapter="offline"))


def followup(api, text="继续核对", **kwargs):
    run = service.followup_run(api.session, api.user, api.run.id, text, **kwargs)
    api.session.flush()
    return run


def test_followup_is_same_conversation_with_ordered_turns(api, model):
    child = followup(api)
    child.status = "succeeded"
    grandchild = service.followup_run(api.session, api.user, child.id, "还没查清的继续查")
    api.session.commit()
    assert api.run.conversation_id == api.run.id
    assert child.conversation_id == grandchild.conversation_id == api.run.id
    assert child.parent_run_id == api.run.id and grandchild.parent_run_id == child.id
    for run, turn in ((api.run, 1), (child, 2), (grandchild, 3)):
        response = api.client.get(f"/api/analysis/runs/{run.id}")
        assert response.status_code == 200, response.text
        assert response.json()["conversation_id"] == api.run.id
        assert response.json()["turn"] == turn
    response = api.client.get(f"/api/analysis/conversations/{api.run.id}")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["turn_count"] == 3 and data["latest_run_id"] == grandchild.id
    assert [r["id"] for r in data["runs"]] == [api.run.id, child.id, grandchild.id]
    assert [r["turn"] for r in data["runs"]] == [1, 2, 3]
    assert api.client.get(f"/api/analysis/conversations/{child.id}").status_code == 404
    # 旧接口继续列出每一轮。
    assert len(api.client.get("/api/analysis/runs").json()) == 3


def test_list_groups_before_limit_and_orders_by_latest_activity(api, model):
    child = followup(api)
    child.finished_at = child.created_at + timedelta(hours=3)
    child.status = "succeeded"
    other = AnalysisRun(
        id="other",
        user_id=api.user.id,
        question="另一问题",
        trigger="manual",
        status="failed",
        created_at=child.created_at + timedelta(hours=1),
    )
    api.session.add(other)
    api.session.commit()
    response = api.client.get("/api/analysis/conversations")
    assert response.status_code == 200, response.text
    data = response.json()
    assert [c["id"] for c in data] == [api.run.id, other.id]
    assert data[0]["title"] == "原报告"
    assert data[0]["turn_count"] == 2 and data[0]["status"] == child.status
    assert data[0]["updated_at"] == child.finished_at.isoformat()
    assert data[1]["title"] == other.question


def test_root_stays_first_when_followup_has_same_timestamp(api):
    child = AnalysisRun(
        id="aaa",
        user_id=api.user.id,
        question="继续核对",
        trigger="manual",
        status="queued",
        conversation_id=api.run.id,
        parent_run_id=api.run.id,
        created_at=api.run.created_at,
    )
    api.session.add(child)
    api.session.commit()
    data = api.client.get(f"/api/analysis/conversations/{api.run.id}").json()
    assert [r["id"] for r in data["runs"]] == [api.run.id, child.id]
    assert [r["turn"] for r in data["runs"]] == [1, 2]
    assert data["latest_run_id"] == child.id
    assert api.client.get("/api/analysis/conversations").json()[0]["latest_run_id"] == child.id


@pytest.mark.parametrize(
    "title,stored", [("  新显示名  ", "新显示名"), ("字" * 80, "字" * 80), ("", None), ("  ", None)]
)
def test_rename_trims_and_clears_without_changing_analysis(api, model, title, stored):
    child = followup(api)
    before = {
        r.id: (r.question, deepcopy(r.report), deepcopy(r.checkpoint), r.conversation_id) for r in (api.run, child)
    }
    response = api.client.patch(f"/api/analysis/conversations/{api.run.id}", json={"display_title": title})
    assert response.status_code == 200, response.text
    assert response.json()["title"] == (stored or "原报告")
    assert response.json()["display_title"] == stored
    assert api.run.display_title == stored and child.display_title is None
    assert before == {r.id: (r.question, r.report, r.checkpoint, r.conversation_id) for r in (api.run, child)}
    event = api.session.scalar(select(AuditEvent).where(AuditEvent.action == "analysis.conversation.rename"))
    assert event is not None and event.object_id == api.run.id
    assert api.client.get("/api/analysis/conversations").json()[0]["title"] == (stored or "原报告")


@pytest.mark.parametrize("payload", [{"display_title": "字" * 81}, {"display_title": None}, {}, {"display_title": 123}])
def test_rename_validates_title(api, payload):
    response = api.client.patch(f"/api/analysis/conversations/{api.run.id}", json=payload)
    assert response.status_code == 422, response.text
    assert api.run.display_title is None


def test_rename_does_not_enter_future_followup_context(api, model):
    from avelis.analysis.conversation_context import refresh_followup_context

    question = api.run.question
    child = followup(api)
    before = refresh_followup_context(api.session, api.user, child)
    response = api.client.patch(
        f"/api/analysis/conversations/{api.run.id}", json={"display_title": "仅供左侧显示的名字"}
    )
    assert response.status_code == 200, response.text
    assert refresh_followup_context(api.session, api.user, child) == before
    after = followup(api).checkpoint["inherited"]
    assert after["user_text"] == child.checkpoint["inherited"]["user_text"]
    assert api.run.question == question
    assert "仅供左侧显示的名字" not in str(after)


def test_conversation_list_is_user_scoped_and_only_initiator_can_rename(api):
    other = User(id="other-user", username="other-user", display_name="另一用户", password_hash="unused")
    admin = User(id="admin", username="admin", display_name="管理员", password_hash="unused")
    api.session.add_all([other, admin, UserRole(user_id=admin.id, role="admin")])
    api.session.commit()
    api.app.dependency_overrides[current_user] = lambda: other
    assert api.client.get("/api/analysis/conversations").json() == []
    assert api.client.get(f"/api/analysis/conversations/{api.run.id}").status_code == 404
    assert (
        api.client.patch(f"/api/analysis/conversations/{api.run.id}", json={"display_title": "改名"}).status_code == 404
    )
    api.app.dependency_overrides[current_user] = lambda: admin
    assert api.client.get("/api/analysis/conversations").json() == []
    assert (
        api.client.patch(f"/api/analysis/conversations/{api.run.id}", json={"display_title": "改名"}).status_code == 403
    )
    assert api.run.display_title is None


def test_scheduled_runs_of_same_task_have_independent_conversations(api, model):
    task = AnalysisTask(owner_id=api.user.id, title="定时核对", question="检查指标变化", status="active")
    api.session.add(task)
    api.session.flush()
    runs = [
        service.create_run(api.session, api.user, task.question, None, task=task, trigger="schedule") for _ in range(2)
    ]
    api.session.commit()
    assert runs[0].conversation_id == runs[0].id != runs[1].conversation_id == runs[1].id
    assert all(r.parent_run_id is None for r in runs)
    data = api.client.get("/api/analysis/conversations").json()
    assert len(data) == 3 and all(c["turn_count"] == 1 for c in data)


def test_followup_after_a_chart_edit_turn_does_not_inherit_the_presentation_scope(api, model):
    """改图轮次的范围只描述呈现：下一轮不当作确认过的分析范围继承，需要新数据的续问才能取数。"""
    child = followup(api, "画成饼图")
    child.status = "succeeded"
    child.report = {"kind": "chart", "dataset": "D1", "preference": {"kind": "donut"}, "figures": []}
    child.plan = {**(child.plan or {}), "scope": {"question_type": "chart", "chart_dataset": "D1", "metric_codes": []}}
    child.checkpoint = {
        **(child.checkpoint or {}),
        "confirmed_scope": {"question_type": "chart", "chart_dataset": "D1"},
    }
    api.session.flush()
    grandchild = service.followup_run(api.session, api.user, child.id, "再看上个月的数据")
    inherited = grandchild.checkpoint["inherited"]
    assert not inherited.get("scope")
    assert grandchild.question == "再看上个月的数据"
