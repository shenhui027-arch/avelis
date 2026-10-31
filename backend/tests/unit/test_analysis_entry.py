"""统一对话入口：真实分析图与运行收尾，模型、问数和元数据会话均用替身。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from avelis.analysis import agent, service, tools
from avelis.analysis.grounding import scope_key
from avelis.api.ask import answer_view
from avelis.api.schemas import AnalysisRunOut, AnswerOut
from avelis.ask import agent as ask_agent
from avelis.ask import service as ask_service
from avelis.db import utcnow
from avelis.models import AnalysisRun, Answer, Job
from tests.unit.test_analysis_hitl import configured as hitl_configured, reply, report_reply, waiting_run
from tests.unit.test_analysis_pipeline import WINDOW, big_catalog, catalog as pipeline_catalog


def scope_reply(question_type="lookup", **extra):
    return reply("submit_scope", {"question_type": question_type, "metric_codes": ["m_a"], **extra}, "scope")


def configured(monkeypatch, responses, **kwargs):
    kwargs.setdefault("catalog", pipeline_catalog())
    return hitl_configured(monkeypatch, responses, **kwargs)


def fake_ask(monkeypatch, box, *, status="answered", explanation="指标甲的值为七"):
    versions = dict(getattr(box, "catalog", ask_service.Catalog([], {})).versions)
    answer = Answer(id="answer-a", question=box.run.question, asked_by=box.user.id, asked_at=utcnow(),
                    status=status, model_adapter="fake-ask", intent={"metric_codes": ["m_a"]}, plan={},
                    sql="SELECT SUM(value) FROM metric_a", metric_version_ids=["metric-v1"],
                    result={"columns": ["指标甲"], "rows": [["7"]], "row_count": 1, "truncated": False},
                    evidence={"sources": []}, freshness={}, message="受治理查询返回指标甲为七", error_code=None,
                    executed_at=utcnow(), duration_ms=3, cache_hit=False, recheck_status=None,
                    thread_id="ask-thread-a", steps=[], explanation=explanation, explanation_source="template")
    calls = []

    def start_answer(session, user, question, adapter, thread_id):
        assert session is box.session and user is box.user and thread_id is None
        answer.question = question
        session.store[(Answer, answer.id)] = answer
        return answer

    def fulfil(session, user, intent, current, catalog):
        assert session is box.session and user is box.user and current is answer
        assert catalog.versions == {code: versions[code] for code in intent.metric_codes}
        calls.append(intent)
        answer.intent = intent.model_dump(mode="json")
        answer.status = status

    monkeypatch.setattr(ask_service, "start_answer", start_answer)
    monkeypatch.setattr(ask_service, "fulfil", fulfil)
    monkeypatch.setattr(ask_service, "finish_answer", lambda session, user, current, started: current)
    monkeypatch.setattr(ask_agent, "run_turn", lambda *a, **kw: pytest.fail("lookup 不应重新调用问数模型"))
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
    assert len(ask_calls) == 1 and prechecks == []
    intent = ask_calls[0]
    assert intent.metric_codes == ["m_a"] and str(intent.start_date) == WINDOW["start"]
    assert str(intent.end_date) == WINDOW["end"]
    assert intent.compare == "custom" and str(intent.compare_start) == WINDOW["compare_start"]
    assert str(intent.compare_end) == WINDOW["compare_end"]
    assert answer.question == run.question
    assert len(model.seen) == run.budget["used"]["model_requests"] == 1
    assert model.tool_choice == ["submit_scope"]
    assert run.report["kind"] == "lookup" and run.report["answer_id"] == answer.id
    assert run.report["title"] == run.question
    assert run.report["summary"] == (answer.explanation or answer.message)
    assert run.verification == {"ok": True, "status": "lookup"}
    assert run.plan["scope"]["question_type"] == "lookup"
    assert run.budget["used"]["tool_calls"] == run.budget["used"]["attempts"] == 1
    assert run.budget["used"]["queries"] == 1
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


@pytest.mark.parametrize("cutoff", ["deadline", "tool_calls", "queries"])
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
            elif cutoff == "queries":
                box.budget.used_queries = box.budget.queries
            else:
                box.budget.used_tools = box.budget.tool_calls - box.budget.report_reserve
        return response

    monkeypatch.setattr(model, "invoke", exhaust)
    agent.build_graph(MemorySaver()).invoke(state, config)
    assert calls == [] and box.report.get("kind") != "lookup"


@pytest.mark.parametrize("unattended", [False, True])
def test_lookup_clarification_uses_the_existing_checkpoint_before_ask(monkeypatch, unattended):
    question = "请确认查询范围？"
    filters = [{"dimension": "地区", "values": ["华东"]}]
    box, model, config, state, prechecks = configured(monkeypatch, [scope_reply(clarification=question),
        scope_reply(filters=filters)], unattended=unattended)
    monkeypatch.setattr(ask_service, "version_sources", lambda session, version: {})
    _, calls = fake_ask(monkeypatch, box)
    graph = agent.build_graph(MemorySaver())
    first = graph.invoke(state, config)
    assert calls == [] and prechecks == [] and len(model.seen) == 1
    if unattended:
        assert first["phase"] == "clarify" and not first.get("__interrupt__")
        assert box.clarification["question"] == question
    else:
        assert first["__interrupt__"][0].value == {"kind": "clarify", "question": question}
        final = graph.invoke(Command(resume={"answer": "只看华东"}), config)
        assert final["phase"] == "done" and len(calls) == 1 and len(model.seen) == 2
        assert final["clarifications"] == [{"question": question, "answer": "只看华东"}]
        assert [f.model_dump() for f in calls[0].filters] == filters
        assert str(calls[0].start_date) == WINDOW["start"] and str(calls[0].end_date) == WINDOW["end"]
        assert box.budget.used_queries == 1
        assert prechecks == [] and box.report["kind"] == "lookup"


def test_lookup_clarification_answers_refresh_and_keep_metric_candidates(monkeypatch):
    catalog = big_catalog(40)
    next(card for card in catalog.cards if card.code == "m_sales").name = "营业收入"
    catalog.versions["m_sales"][0].name = "营业收入"
    questions = ["要分析哪个指标？", "请确认查询地区？"]
    filters = [{"dimension": "地区", "values": ["华东"]}]
    box, model, config, state, prechecks = configured(monkeypatch, [
        scope_reply(metric_codes=[], clarification=questions[0]),
        scope_reply(metric_codes=["m_sales"], clarification=questions[1]),
        scope_reply(metric_codes=["m_sales"], filters=filters)], catalog=catalog)
    box.run.question = state["question"] = "为什么下降？"
    assert len(catalog.cards) > box.ALL_CANDIDATES
    bare_candidates = box.candidate_metrics(box.run.question)
    assert len(bare_candidates) == box.CANDIDATES
    assert "m_sales" not in {candidate["code"] for candidate in bare_candidates}
    monkeypatch.setattr(ask_service, "version_sources", lambda session, version: {})
    _, calls = fake_ask(monkeypatch, box)
    graph = agent.build_graph(MemorySaver())

    first = graph.invoke(state, config)
    assert first["__interrupt__"][0].value == {"kind": "clarify", "question": questions[0]}
    second = graph.invoke(Command(resume={"answer": "营业收入"}), config)
    assert second["__interrupt__"][0].value == {"kind": "clarify", "question": questions[1]}
    final = graph.invoke(Command(resume={"answer": "华东"}), config)

    assert final["phase"] == "done" and not final.get("__interrupt__")
    assert final["clarifications"] == [{"question": questions[0], "answer": "营业收入"},
                                       {"question": questions[1], "answer": "华东"}]
    assert box.scope.metric_codes == ["m_sales"] and box.clarification is None
    assert box.report["kind"] == "lookup" and prechecks == [] and len(calls) == 1
    assert calls[0].metric_codes == ["m_sales"]
    assert [f.model_dump() for f in calls[0].filters] == filters
    assert len(model.seen) == box.budget.used_model_requests == 3
    for messages in model.seen[1:]:
        prompt = next(message.content for message in messages if isinstance(message, HumanMessage))
        candidates = json.loads(next(line.removeprefix("候选口径：") for line in prompt.splitlines()
                                     if line.startswith("候选口径：")))
        assert "m_sales" in {candidate["code"] for candidate in candidates}


@pytest.mark.parametrize("order", ["asc", "desc"])
@pytest.mark.parametrize("scope_window", [False, True])
def test_lookup_maps_understood_window_and_ranking_to_intent(monkeypatch, order, scope_window):
    window = {"start": "2026-10-05", "end": "2026-10-12"}
    dates = {"start_date": "2026-07-01", "end_date": "2026-07-31",
             "compare_start": "2026-06-01", "compare_end": "2026-06-30"} if scope_window else {}
    filters = [{"dimension": "渠道", "values": ["直营", "加盟"]}]
    box, model, config, state, prechecks = configured(monkeypatch, [scope_reply(
        dimensions=["地区"], filters=filters, order=order, limit=3, **dates)])
    box.run.window = window
    state["context"]["window"] = window
    _, calls = fake_ask(monkeypatch, box)

    final = agent.build_graph(MemorySaver()).invoke(state, config)

    assert final["phase"] == "done" and prechecks == [] and len(calls) == 1
    intent = calls[0]
    assert intent.metric_codes == ["m_a"] and intent.group_by == ["地区"]
    assert [f.model_dump() for f in intent.filters] == filters
    assert intent.order == order and intent.limit == 3
    assert str(intent.start_date) == dates.get("start_date", window["start"])
    assert str(intent.end_date) == dates.get("end_date", window["end"])
    if scope_window:
        assert intent.compare == "custom"
        assert str(intent.compare_start) == dates["compare_start"]
        assert str(intent.compare_end) == dates["compare_end"]
    else:
        assert intent.compare is None and intent.compare_start is None and intent.compare_end is None
    assert len(model.seen) == box.budget.used_model_requests == box.budget.used_queries == 1


@pytest.mark.parametrize("question,time_grain", [
    ("9 月每天的产量", "day"),
    ("今年每月的产量", "month"),
    ("9 月的产量", None),
])
def test_lookup_maps_understood_time_grain_to_intent(monkeypatch, question, time_grain):
    scope = {"time_grain": time_grain} if time_grain else {}
    box, model, config, state, prechecks = configured(monkeypatch, [scope_reply(**scope)])
    box.run.question = state["question"] = question
    window = {"start": "2026-09-01", "end": "2026-09-30"}
    box.run.window = state["context"]["window"] = window
    _, calls = fake_ask(monkeypatch, box)

    final = agent.build_graph(MemorySaver()).invoke(state, config)

    assert final["phase"] == "done" and prechecks == [] and len(calls) == 1
    assert calls[0].time_grain == time_grain
    assert box.plan["scope"]["time_grain"] == time_grain
    assert len(model.seen) == box.budget.used_model_requests == box.budget.used_queries == 1


@pytest.mark.parametrize("scope,error", [
    ({"compare_start": "2026-08-31", "compare_end": "2026-08-01"}, "compare_start"),
    ({"time_grain": "day", "compare_start": "2026-08-01", "compare_end": "2026-08-31"},
     "按时间分组时不能同时"),
    ({"metric_codes": []}, "取数范围没有指标"),
])
def test_invalid_lookup_intent_falls_back_without_asking_or_spending_a_query(monkeypatch, scope, error):
    box, model, config, state, prechecks = configured(monkeypatch, [scope_reply(**scope),
        reply("submit_proposal", {"items": []}, "proposal"), report_reply()])
    _, calls = fake_ask(monkeypatch, box)

    final = agent.build_graph(MemorySaver()).invoke(state, config)

    assert final["phase"] == "done" and not final.get("__interrupt__")
    assert calls == [] and prechecks == ["precheck"]
    assert box.report.get("kind") != "lookup" and box.verification["ok"]
    assert box.budget.used_queries == 1  # 唯一的查询来自完整分析预检。
    assert model.tool_choice == ["submit_scope", "submit_proposal", None]
    ask_step = next(step for step in box.session.added if getattr(step, "tool", None) == "ask")
    assert ask_step.status == "error" and error in ask_step.output["error"]


def test_lookup_refreshes_requested_metric_when_the_toolbox_catalog_is_empty(monkeypatch):
    box, model, config, _, prechecks = configured(monkeypatch, [])
    _, calls = fake_ask(monkeypatch, box)
    box.catalog = ask_service.Catalog([], {})

    result = agent.lookup({"question": box.run.question, "scope": scope_reply().tool_calls[0]["args"]}, config)

    assert result["phase"] == "done" and prechecks == [] and model.seen == []
    assert len(calls) == 1 and calls[0].metric_codes == ["m_a"]
    assert box.budget.used_queries == 1 and box.report["kind"] == "lookup"


@pytest.mark.parametrize("limit", [0, 101])
def test_lookup_scope_rejects_ranking_limits_outside_the_ask_bound(limit):
    with pytest.raises(ValueError):
        tools.Scope(question_type="lookup", metric_codes=["m_a"], limit=limit)


def test_lookup_does_not_query_if_authorization_uses_the_remaining_time(monkeypatch):
    box, _, config, _, _ = configured(monkeypatch, [])
    _, calls = fake_ask(monkeypatch, box)
    metric = box._metric

    def expire(code):
        result = metric(code)
        box.budget.started -= box.budget.seconds + 1
        return result

    monkeypatch.setattr(box, "_metric", expire)
    result = agent.lookup({"question": box.run.question, "scope": scope_reply().tool_calls[0]["args"]}, config)

    assert result["phase"] == "precheck" and calls == [] and box.budget.used_queries == 0


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


@pytest.mark.parametrize("scope_location", ["scope", "window", "missing"])
@pytest.mark.parametrize("text,start,end,region,dimensions", [
    ("改查 2026 年 8 月华南", "2026-08-01", "2026-08-31", "华南", []),
    ("那华南呢", "2026-09-01", "2026-09-30", "华南", []),
    ("按产线拆开", "2026-09-01", "2026-09-30", "华东", ["产线"]),
])
def test_lookup_followup_inherits_understood_scope_or_understands_new_question(
        monkeypatch, scope_location, text, start, end, region, dimensions):
    session, user, parent, _ = waiting_run(monkeypatch)
    parent.question = "2026 年 9 月华东订单量"
    previous_window = {"start": "2026-09-01", "end": "2026-09-30"}
    understood = {"question_type": "lookup", "metric_codes": ["m_a"], "dimensions": [],
                  "filters": [{"dimension": "地区", "values": ["华东"]}]}
    if scope_location == "scope":
        understood.update(start_date=previous_window["start"], end_date=previous_window["end"])
    parent.window = {} if scope_location == "scope" else previous_window
    parent.status, parent.report = "succeeded", {"kind": "lookup", "answer_id": "original-answer"}
    parent.plan = {"scope": understood} if scope_location != "missing" else {}
    parent.checkpoint = {"confirmed_scope": {"question_type": "change", "metric_codes": []},
                         "confirmed_items": [{"id": "P1", "content": "上一轮已确认项"}]}
    monkeypatch.setattr(service, "require_analysis_model", lambda s: SimpleNamespace(adapter="fake"))
    monkeypatch.setattr(service.access, "require_role", lambda *a: None)
    child = service.followup_run(session, user, parent.id, text)
    assert child.parent_run_id == parent.id and child.question == text
    assert child.window == {}
    inherited = child.checkpoint["inherited"]
    expected_scope = understood if scope_location != "missing" else {}
    assert inherited == {"user_text": text, "previous_question": parent.question,
                         "previous_scope": expected_scope, "previous_window": parent.window}

    filters = [{"dimension": "地区", "values": [region]}]
    box, model, config, _, prechecks = configured(monkeypatch, [scope_reply(
        start_date=start, end_date=end, filters=filters, dimensions=dimensions)])
    box.run.question = child.question
    box.run.window = child.window
    candidate_questions = []
    candidate_metrics = box.candidate_metrics

    def candidates(question):
        candidate_questions.append(question)
        return candidate_metrics(question)

    monkeypatch.setattr(box, "candidate_metrics", candidates)
    _, calls = fake_ask(monkeypatch, box)
    state = agent.initial_state(child.question, "2026-10-31", "UTC", child.window,
                                inherited=inherited)
    final = agent.build_graph(MemorySaver()).invoke(state, config)
    assert final["phase"] == "done" and box.report["kind"] == "lookup" and prechecks == []
    assert len(calls) == 1 and calls[0].metric_codes == ["m_a"]
    intent = calls[0]
    assert str(intent.start_date) == start and str(intent.end_date) == end
    assert [f.model_dump() for f in intent.filters] == filters and intent.group_by == dimensions
    assert intent.compare is None and intent.compare_start is None and intent.compare_end is None
    assert len(model.seen) == box.budget.used_model_requests == 1
    assert len(candidate_questions) == 1
    assert parent.question in candidate_questions[0] and text in candidate_questions[0]
    prompt = next(message.content for message in model.seen[0] if isinstance(message, HumanMessage))
    lines = dict(line.split("：", 1) for line in prompt.splitlines() if "：" in line)
    assert lines["用户问题"] == text and lines["上一轮问题"] == parent.question
    assert json.loads(lines["上一轮理解的范围"]) == expected_scope
    assert json.loads(lines["上一轮窗口"]) == parent.window
    assert "未提到的部分沿用上一轮" in prompt and "相对时间仍按今天计算" in prompt
    assert "2026-10-31" in prompt


def test_lookup_followup_keeps_metric_candidates_across_short_questions(monkeypatch):
    filters = [{"dimension": "地区", "values": ["华南"]}]
    previous_scope = {"question_type": "lookup", "metric_codes": ["m_units"], "filters": filters,
                      "start_date": "2026-09-01", "end_date": "2026-09-30"}
    box, model, config, _, prechecks = configured(monkeypatch,
        [scope_reply(**previous_scope, dimensions=["产线"])], catalog=big_catalog(40))
    box.run.question, box.run.window = "按产线拆开", {}
    bare_candidates = box.candidate_metrics(f"那华南呢\n{box.run.question}")
    assert len(bare_candidates) == box.CANDIDATES
    assert "m_units" not in {candidate["code"] for candidate in bare_candidates}
    _, calls = fake_ask(monkeypatch, box)
    inherited = {"user_text": box.run.question, "previous_question": "那华南呢",
                 "previous_scope": previous_scope, "previous_window": {}}
    state = agent.initial_state(box.run.question, "2026-10-31", "UTC", box.run.window, inherited=inherited)

    final = agent.build_graph(MemorySaver()).invoke(state, config)

    assert final["phase"] == "done" and not final.get("__interrupt__")
    assert box.report["kind"] == "lookup" and box.clarification is None and prechecks == []
    assert len(model.seen) == box.budget.used_model_requests == 1
    prompt = next(message.content for message in model.seen[0] if isinstance(message, HumanMessage))
    candidates = json.loads(next(line.removeprefix("候选口径：") for line in prompt.splitlines()
                                 if line.startswith("候选口径：")))
    assert "m_units" in {candidate["code"] for candidate in candidates}
    assert len(calls) == 1
    intent = calls[0]
    assert intent.metric_codes == ["m_units"] and intent.group_by == ["产线"]
    assert [f.model_dump() for f in intent.filters] == filters
    assert str(intent.start_date) == "2026-09-01" and str(intent.end_date) == "2026-09-30"


@pytest.mark.parametrize("cutoff", ["deadline", "model_requests"])
def test_lookup_followup_does_not_reuse_previous_scope_without_a_model_call(monkeypatch, cutoff):
    box, model, config, _, prechecks = configured(monkeypatch, [])
    box.run.question, box.run.window = "那华南呢", {}
    _, calls = fake_ask(monkeypatch, box)
    inherited = {"user_text": box.run.question, "previous_question": "2026 年 9 月华东订单量",
                 "previous_scope": {"question_type": "lookup", "metric_codes": ["m_a"],
                                    "filters": [{"dimension": "地区", "values": ["华东"]}],
                                    "start_date": "2026-09-01", "end_date": "2026-09-30"},
                 "previous_window": {}}
    state = agent.initial_state(box.run.question, "2026-10-31", "UTC", box.run.window, inherited=inherited)
    if cutoff == "deadline":
        box.budget.started -= box.budget.seconds + 1
    else:
        box.budget.used_model_requests = box.budget.model_requests

    result = agent.understand(state, config)

    assert result["phase"] == "precheck" and result["scope"] is None
    assert box.scope is None and box.plan["scope"] is None
    assert model.seen == [] and calls == [] and prechecks == []
