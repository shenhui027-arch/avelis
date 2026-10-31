"""统一对话入口：真实分析图与运行收尾，模型、问数和元数据会话均用替身。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from avelis.analysis import agent, service, tools
from avelis.analysis.grounding import scope_key
from avelis.api.ask import answer_view
from avelis.api.schemas import AnalysisRunOut, AnswerOut
from avelis.ask import agent as ask_agent
from avelis.db import utcnow
from avelis.models import AnalysisRun, Answer, Job
from tests.unit.test_analysis_hitl import configured, reply, report_reply, waiting_run
from tests.unit.test_analysis_pipeline import WINDOW, catalog as pipeline_catalog


def scope_reply(question_type="lookup", **extra):
    return reply("submit_scope", {"question_type": question_type, "metric_codes": [], **extra}, "scope")


def fake_ask(monkeypatch, box, *, status="answered", explanation="指标甲的值为七"):
    answer = Answer(id="answer-a", question=box.run.question, asked_by=box.user.id, asked_at=utcnow(),
                    status=status, model_adapter="fake-ask", intent={"metric_codes": ["m_a"]}, plan={},
                    sql="SELECT SUM(value) FROM metric_a", metric_version_ids=["metric-v1"],
                    result={"columns": ["指标甲"], "rows": [["7"]], "row_count": 1, "truncated": False},
                    evidence={"sources": []}, freshness={}, message="受治理查询返回指标甲为七", error_code=None,
                    executed_at=utcnow(), duration_ms=3, cache_hit=False, recheck_status=None,
                    thread_id="ask-thread-a", steps=[], explanation=explanation, explanation_source="template")
    calls = []

    def run_turn(session, user, question, thread_id):
        calls.append((session, user, question, thread_id))
        answer.question = question
        session.store[(Answer, answer.id)] = answer
        return answer

    monkeypatch.setattr(ask_agent, "run_turn", run_turn)
    return answer, calls


def answer_scalar(session, answer):
    """让内存会话执行 Answer 的身份过滤；缺少 owner 条件也会被测试发现。"""
    def scalar(statement):
        params = statement.compile().params
        if params.get("id_1") == answer.id and params.get("asked_by_1") == answer.asked_by:
            return session.store.get((Answer, answer.id))
        return None

    session.scalar = scalar


@pytest.mark.parametrize("trigger,explanation", [("manual", "指标甲的值为七"), ("schedule", None)])
def test_lookup_runs_ask_once_and_finishes_without_analysis(monkeypatch, trigger, explanation):
    box, model, _, _, prechecks = configured(monkeypatch, [scope_reply()], unattended=trigger == "schedule")
    session, user = box.session, box.user
    user.username = "user-a"
    run = AnalysisRun(id="lookup-run", user_id=user.id, question="指标甲是多少？", status="queued", trigger=trigger,
                      confirmation_mode="always", window=dict(WINDOW), budget=box.budget.view(), checkpoint={},
                      plan={}, report={}, verification={}, stop_requested=False, created_at=utcnow())
    box.run = run
    session.store[(AnalysisRun, run.id)] = run
    session.rollback = lambda: None
    job = Job(id="lookup-job", status="running", attempts=1, kind="analysis_run", result={},
              payload={"run_id": run.id})
    answer, ask_calls = fake_ask(monkeypatch, box, explanation=explanation)
    session.scalar = lambda statement: job if job.status == "running" else None

    def flush():
        for index, obj in enumerate(session.added):
            if getattr(obj, "id", None) is None:
                obj.id = f"object-{index}"
            session.store[(type(obj), obj.id)] = obj

    def execute(statement):
        if getattr(statement, "is_update", False):
            if statement.compile().params.get("status_1") != run.status:
                return SimpleNamespace(rowcount=0)
            for key, value in statement._values.items():
                setattr(run, key.key if hasattr(key, "key") else key, value.value)
            return SimpleNamespace(rowcount=1)
        return SimpleNamespace(one_or_none=lambda: SimpleNamespace(status=run.status, budget=run.budget))

    session.flush, session.execute = flush, execute
    graph = agent.build_graph(MemorySaver())
    model.adapter = "fake-analysis"
    monkeypatch.setattr(service, "warm_up", lambda: None)
    monkeypatch.setattr(service, "_graph", lambda: graph)
    monkeypatch.setattr(service, "require_analysis_model", lambda s: model)
    monkeypatch.setattr(tools.AnalysisToolbox, "precheck", lambda *a: pytest.fail("lookup 不应执行 precheck"))
    outcome = service.execute(session, run.id, job.id, 1)

    assert outcome["status"] == run.status == job.status == "succeeded"
    assert ask_calls == [(session, user, run.question, None)] and prechecks == []
    assert len(model.seen) == run.budget["used"]["model_requests"] == 1
    assert model.tool_choice == ["submit_scope"]
    assert run.report["kind"] == "lookup" and run.report["answer_id"] == answer.id
    assert run.report["title"] == run.question
    assert run.report["summary"] == (answer.explanation or answer.message)
    assert run.verification == {"ok": True, "status": "lookup"}
    assert run.plan["scope"]["question_type"] == "lookup"
    assert run.budget["used"]["tool_calls"] == run.budget["used"]["attempts"] == 1
    ask_steps = [step for step in session.added if getattr(step, "tool", None) == "ask"]
    assert len(ask_steps) == 1
    assert ask_steps[0].output["answer_id"] == answer.id and ask_steps[0].output["status"] == "answered"

    answer_scalar(session, answer)
    view = service.run_view(session, run, user=user)
    assert view["answer"] == answer_view(answer)
    assert set(view["answer"]) == set(AnswerOut.model_fields)
    assert AnalysisRunOut.model_validate(view).answer == AnswerOut.model_validate(view["answer"]).model_dump()


@pytest.mark.parametrize("status", ["clarify", "blocked", "denied", "error"])
def test_unanswered_lookup_falls_back_to_full_analysis(monkeypatch, status):
    box, model, config, state, prechecks = configured(monkeypatch, [scope_reply(),
        reply("submit_proposal", {"items": []}, "proposal"), report_reply()])
    answer, calls = fake_ask(monkeypatch, box, status=status)
    final = agent.build_graph(MemorySaver()).invoke(state, config)
    assert final["phase"] == "done" and not final.get("__interrupt__")
    assert len(calls) == 1 and prechecks == ["precheck"]
    assert model.tool_choice == ["submit_scope", "submit_proposal", None] and len(model.seen) == 3
    assert box.report.get("kind") != "lookup" and box.verification["ok"]
    steps = [step for step in box.session.added if getattr(step, "tool", None) == "ask"]
    assert len(steps) == 1 and steps[0].output["answer_id"] == answer.id
    assert steps[0].output["status"] == status
    assert box.budget.used_tools == 1 and box.budget.used_calls == 2  # ask 与 submit_report


@pytest.mark.parametrize("question_type", ["change", "relation"])
def test_analysis_question_keeps_existing_path(monkeypatch, question_type):
    report = report_reply()
    if question_type == "change":
        report.tool_calls[0]["args"]["scope"] = {"metric_code": "m_a"}
    box, model, config, state, prechecks = configured(monkeypatch,
        [scope_reply(question_type, metric_codes=["m_a"]),
         reply("submit_proposal", {"items": []}, "proposal"), report], catalog=pipeline_catalog())
    if question_type == "change":
        # 替身预检只有单期值：明确登记自动归因不适用，报告保留“证据不足”。
        box.explanations[scope_key("m_a", [], WINDOW)] = "unsupported"
    _, calls = fake_ask(monkeypatch, box)
    final = agent.build_graph(MemorySaver()).invoke(state, config)
    assert final["phase"] == "done" and prechecks == ["precheck"] and calls == []
    assert model.tool_choice == ["submit_scope", "submit_proposal", None] and len(model.seen) == 3
    assert box.scope.question_type == question_type and box.verification["ok"]
    assert box.budget.used_tools == 0 and not any(getattr(s, "tool", None) == "ask" for s in box.session.added)


@pytest.mark.parametrize("cutoff", ["deadline", "tool_calls"])
def test_lookup_does_not_call_ask_after_budget_cutoff(monkeypatch, cutoff):
    box, model, config, state, _ = configured(monkeypatch, [scope_reply(),
        reply("submit_proposal", {"items": []}, "proposal"), report_reply()])
    _, calls = fake_ask(monkeypatch, box)
    invoke = model.invoke

    def exhaust(messages):
        response = invoke(messages)
        if len(model.seen) == 1:
            if cutoff == "deadline":
                box.budget.started -= box.budget.seconds + 1
            else:
                box.budget.used_tools = box.budget.tool_calls - box.budget.report_reserve
        return response

    monkeypatch.setattr(model, "invoke", exhaust)
    agent.build_graph(MemorySaver()).invoke(state, config)
    assert calls == [] and box.report.get("kind") != "lookup"


@pytest.mark.parametrize("unattended", [False, True])
def test_lookup_clarification_uses_the_existing_checkpoint_before_ask(monkeypatch, unattended):
    question = "请确认查询范围？"
    box, model, config, state, prechecks = configured(monkeypatch, [scope_reply(clarification=question),
        scope_reply()], unattended=unattended)
    _, calls = fake_ask(monkeypatch, box)
    graph = agent.build_graph(MemorySaver())
    first = graph.invoke(state, config)
    assert calls == [] and prechecks == [] and len(model.seen) == 1
    if unattended:
        assert first["phase"] == "clarify" and not first.get("__interrupt__")
        assert box.clarification["question"] == question
    else:
        assert first["__interrupt__"][0].value == {"kind": "clarify", "question": question}
        final = graph.invoke(Command(resume={"answer": "本期全部范围"}), config)
        assert final["phase"] == "done" and len(calls) == 1 and len(model.seen) == 2
        assert final["clarifications"] == [{"question": question, "answer": "本期全部范围"}]
        assert prechecks == [] and box.report["kind"] == "lookup"


@pytest.mark.parametrize("viewer", ["other", "admin", None])
def test_run_view_does_not_expose_another_users_lookup_answer(monkeypatch, viewer):
    session, user, run, _ = waiting_run(monkeypatch)
    box = SimpleNamespace(session=session, user=user, run=run)
    answer, _ = fake_ask(monkeypatch, box)
    session.store[(Answer, answer.id)] = answer
    run.created_at = utcnow()
    run.report = {"kind": "lookup", "answer_id": answer.id, "title": run.question, "summary": answer.message}
    run.verification = {"ok": True, "status": "lookup"}
    answer_scalar(session, answer)
    monkeypatch.setattr(service.access, "is_admin", lambda s, u: viewer == "admin")
    current = SimpleNamespace(id=viewer) if viewer else None
    view = service.run_view(session, run, user=current)
    assert view.get("answer") is None and AnalysisRunOut.model_validate(view).answer is None
    assert service.run_view(session, run, user=user)["answer"] == answer_view(answer)
    assert service.run_view(session, run, user=user, full=False).get("answer") is None


def test_run_view_handles_a_missing_lookup_answer(monkeypatch):
    session, user, run, _ = waiting_run(monkeypatch)
    run.created_at, run.report = utcnow(), {"kind": "lookup", "answer_id": "missing"}
    assert service.run_view(session, run, user=user).get("answer") is None


@pytest.mark.parametrize("has_scope", [False, True])
def test_lookup_followup_inherits_understood_scope_or_understands_new_question(monkeypatch, has_scope):
    session, user, parent, _ = waiting_run(monkeypatch)
    understood = {"question_type": "lookup", "metric_codes": [], "filters": []}
    parent.status, parent.report = "succeeded", {"kind": "lookup", "answer_id": "original-answer"}
    parent.plan = {"scope": understood} if has_scope else {}
    parent.checkpoint = {"confirmed_scope": {"question_type": "change", "metric_codes": []}} if has_scope else {}
    monkeypatch.setattr(service, "require_analysis_model", lambda s: SimpleNamespace(adapter="fake"))
    monkeypatch.setattr(service.access, "require_role", lambda *a: None)
    text = "换成上期的指标甲是多少？"
    child = service.followup_run(session, user, parent.id, text)
    assert child.parent_run_id == parent.id and child.question == text
    assert child.checkpoint["inherited"]["scope"] == (understood if has_scope else {})
    assert child.checkpoint["inherited"]["user_text"] == text

    box, model, config, _, prechecks = configured(monkeypatch, [] if has_scope else [scope_reply()])
    box.run.question = child.question
    _, calls = fake_ask(monkeypatch, box)
    state = agent.initial_state(child.question, "2026-10-31", "UTC", child.window,
                                inherited=child.checkpoint["inherited"])
    final = agent.build_graph(MemorySaver()).invoke(state, config)
    assert final["phase"] == "done" and box.report["kind"] == "lookup" and prechecks == []
    assert len(calls) == 1 and calls[0][2:] == (text, None)
    assert len(model.seen) == (0 if has_scope else 1)
