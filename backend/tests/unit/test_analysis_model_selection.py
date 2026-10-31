"""按次模型选择：真实解析、运行与恢复，外部模型和元数据会话使用替身。"""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver

from avelis import jobs, modelcfg
from avelis.analysis import agent, service, tools
from avelis.governance.errors import GovernanceError
from avelis.models import AnalysisRun, Job, ModelAssignment, ModelProvider
from tests.unit.test_analysis_hitl import clarify_reply, reply, report_reply, scope_reply
from tests.unit.test_analysis_live import LiveSession
from tests.unit.test_analysis_pipeline import Phased
from tests.unit.test_analysis_toolbox import governed, make_box


@pytest.fixture
def selection(monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    session, user = LiveSession.replace(box.session), box.user
    user.username = "unit-user"
    provider = ModelProvider(id="selected-provider", code="deepseek-unit", name="DeepSeek",
                             kind="openai_compatible", preset="deepseek", base_url="https://unit.invalid/v1",
                             api_key_enc="encrypted-test-key", enabled=True,
                             models=[{"id": name, "enabled": True} for name in ("chosen", "other")],
                             last_check={"models": {name: {"ok": True, "tool_calling": True}
                                                    for name in ("chosen", "other")}})
    session.store[(ModelProvider, provider.id)] = provider
    model = Phased([])
    built = []

    def build(current, model_id, **kwargs):
        built.append((current.id, model_id, kwargs))
        return model

    monkeypatch.setattr(modelcfg, "build_chat_model", build)
    monkeypatch.setattr(modelcfg, "decrypt_secret", lambda token: "offline-key")
    monkeypatch.setattr(service, "warm_up", lambda: None)
    return session, user, provider, model, built


def default_assignment(session, provider, *, model_id="other", effort="low"):
    assignment = ModelAssignment(purpose="ask", provider_id=provider.id, model_id=model_id,
                                 reasoning_effort=effort)
    session.store[(ModelAssignment, assignment.purpose)] = assignment


def test_selected_model_is_saved_and_used_before_and_after_checkpoint_resume(monkeypatch, selection):
    session, user, provider, model, built = selection
    choice = {"provider_id": provider.id, "model_id": "chosen", "reasoning_effort": "high"}
    run = service.create_run(session, user, "检查指标变化", None,
                             model={"provider_id": provider.id, "model_id": "chosen"}, reasoning_effort="high")
    assert run.model_choice == choice  # 没有默认指派，显式选择仍可发起。
    model.replies[:] = [clarify_reply("请确认地区？"), scope_reply(),
                        reply("submit_proposal", {"items": []}, "proposal"), report_reply()]
    graph = agent.build_graph(MemorySaver())
    monkeypatch.setattr(service, "_graph", lambda: graph)

    def precheck(current, scope):
        current.scope = scope
        return [f"D{governed(current, ['值'], [['7']]).seq}"]

    monkeypatch.setattr(tools.AnalysisToolbox, "precheck", precheck)
    job = Job(id="first-job", kind="analysis_run", status="running", attempts=1,
              payload={"run_id": run.id}, result={})
    session.add(job)
    session.flush()
    first = service.execute(session, run.id, job.id, 1)
    assert first["run_status"] == run.status == "waiting"
    assert run.model_choice == choice and run.model_adapter == "deepseek-unit:chosen"
    assert run.reasoning_effort == "high" and len(model.seen) == 1

    # 暂停期间默认指派变化；恢复仍须使用运行保存的选择。
    default_assignment(session, provider, model_id="other", effort="max")
    resumed = Job(id="resumed-job", kind="analysis_run", status="queued", attempts=1,
                  payload={"run_id": run.id}, result={})
    session.add(resumed)
    monkeypatch.setattr(jobs, "_enqueue", lambda *args, **kwargs: resumed.id)
    service.resume_run(session, user, run.id, {"answer": "华东"})
    resumed.status = "running"
    final = service.execute(session, run.id, resumed.id, 1)
    assert final["status"] == run.status == "succeeded" and len(model.seen) == 4
    assert run.model_choice == choice and run.reasoning_effort == "high"
    assert run.model_adapter == "deepseek-unit:chosen"
    assert built and {(provider_id, model_id) for provider_id, model_id, _ in built} == {(provider.id, "chosen")}
    assert any(kwargs["effort"] == "high" for _, _, kwargs in built)
    assert service.run_view(session, run, full=False)["model_choice"] == choice


@pytest.mark.parametrize("trigger", ["manual", "schedule"])
def test_run_without_choice_keeps_default_assignment_and_null_choice(selection, trigger):
    session, user, provider, _, built = selection
    default_assignment(session, provider, effort="max")
    run = service.create_run(session, user, "检查指标变化", None, trigger=trigger)
    assert run.model_choice is None
    assert [(model_id, kwargs["effort"]) for _, model_id, kwargs in built] == [("other", "max")]
    assert service.run_view(session, run, full=False)["model_choice"] is None


@pytest.mark.parametrize("selection_kwargs,expected_model,expected_effort", [
    ({}, "chosen", "high"),
    ({"reasoning_effort": "max"}, "chosen", "max"),
    ({"model": {"provider_id": "selected-provider", "model_id": "other"}}, "other", "low"),
    ({"model": {"provider_id": "selected-provider", "model_id": "other"},
      "reasoning_effort": "off"}, "other", "off"),
])
def test_followup_inherits_or_replaces_user_choice(selection, selection_kwargs, expected_model, expected_effort):
    session, user, provider, _, _ = selection
    parent = service.create_run(session, user, "检查指标变化", None,
                                model={"provider_id": provider.id, "model_id": "chosen"}, reasoning_effort="high")
    parent.status = "succeeded"
    parent.plan, parent.report, parent.verification = {}, {}, {}
    original = dict(parent.model_choice)
    child = service.followup_run(session, user, parent.id, "继续核对", **selection_kwargs)
    assert child.parent_run_id == parent.id
    assert child.model_choice == {"provider_id": provider.id, "model_id": expected_model,
                                  "reasoning_effort": expected_effort}
    assert parent.model_choice == original


def test_followup_preserves_model_only_choice_without_verified_efforts(selection):
    session, user, provider, _, _ = selection
    provider.preset = None
    parent = service.create_run(session, user, "检查指标变化", None,
                                model={"provider_id": provider.id, "model_id": "chosen"})
    parent.status = "succeeded"
    parent.plan, parent.report, parent.verification = {}, {}, {}
    assert modelcfg.effort_options(provider, "chosen") == []
    child = service.followup_run(session, user, parent.id, "继续核对")
    assert child.model_choice == parent.model_choice
    assert child.model_choice is not parent.model_choice
    assert service.require_analysis_model(session, child.model_choice).effective_effort is None
    # 继承不把隐含默认值当作用户提交的 effort；用户显式提交仍按原规则校验。
    with pytest.raises(GovernanceError) as exc:
        service.followup_run(session, user, parent.id, "继续核对", reasoning_effort="low")
    assert exc.value.status_code == 400


def test_followup_rejects_inherited_choice_that_is_no_longer_available(selection):
    session, user, provider, _, _ = selection
    parent = service.create_run(session, user, "检查指标变化", None,
                                model={"provider_id": provider.id, "model_id": "chosen"})
    parent.status = "succeeded"
    parent.plan, parent.report, parent.verification = {}, {}, {}
    provider.enabled = False
    with pytest.raises(GovernanceError) as exc:
        service.followup_run(session, user, parent.id, "继续核对")
    assert exc.value.status_code == 400


@pytest.mark.parametrize("selection_kwargs", [
    {"model": {"provider_id": "missing", "model_id": "chosen"}},
    {"model": {"provider_id": "selected-provider", "model_id": "disabled"}},
    {"model": {"provider_id": "selected-provider", "model_id": "chosen"}, "reasoning_effort": "unknown"},
])
def test_invalid_choice_is_rejected_before_run_is_added(selection, selection_kwargs):
    session, user, _, _, built = selection
    with pytest.raises(GovernanceError) as exc:
        service.create_run(session, user, "检查指标变化", None, **selection_kwargs)
    assert exc.value.status_code == 400
    assert not built and not any(isinstance(row, AnalysisRun) for row in session.added)
