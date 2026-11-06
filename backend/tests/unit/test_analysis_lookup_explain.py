"""取数快路径复用问数解释：内存会话与假模型，不连接数据库或外部服务。"""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from avelis import modelcfg
from avelis.analysis import agent
from avelis.ask import agent as ask_agent
from avelis.ask import service as ask_service
from avelis.i18n import reply_language_instruction
from tests.unit.test_analysis_entry import configured, fake_ask, scope_reply

LEAKED_TOOL_MARKUP = (
    '<｜DSML｜function_calls><｜DSML｜invoke name="chart">'
    '<｜DSML｜parameter name="kind" string="true">bar</｜DSML｜parameter>'
    "</｜DSML｜invoke></｜DSML｜function_calls>"
)


def setup_lookup(monkeypatch, replies=(), *, send=True, configured_model=True, tools=False):
    box, analysis_model, config, state, prechecks = configured(monkeypatch, [])
    answer, calls = fake_ask(monkeypatch, box, explanation=None)
    state["scope"] = scope_reply().tool_calls[0]["args"]
    state["context"]["request_locale"] = "en"
    monkeypatch.setattr(agent, "get_settings", lambda: SimpleNamespace(reply_language="auto"))
    model = Mock()
    model.invoke.side_effect = list(replies)
    model.bind_tools.return_value = Mock(invoke=Mock(side_effect=model.invoke))
    policy = Mock(return_value={"send_results": send})
    resolve = Mock(return_value=SimpleNamespace(model=model) if configured_model else None)
    monkeypatch.setattr(modelcfg, "policy", policy)
    monkeypatch.setattr(modelcfg, "resolve", resolve)
    monkeypatch.setattr(ask_agent, "_explain_supports_tools", lambda *_: tools)
    persisted = []

    def finish(session, user, current, started):
        assert session is box.session and user is box.user and current is answer
        persisted.append(
            {
                "explanation": current.explanation,
                "source": current.explanation_source,
                "steps": deepcopy(current.steps),
                "evidence": deepcopy(current.evidence),
            }
        )
        return current

    monkeypatch.setattr(ask_service, "finish_answer", finish)
    return SimpleNamespace(
        box=box,
        analysis_model=analysis_model,
        config=config,
        state=state,
        prechecks=prechecks,
        answer=answer,
        calls=calls,
        model=model,
        policy=policy,
        resolve=resolve,
        persisted=persisted,
    )


def assert_answered_lookup(case, source):
    assert case.answer.status == "answered" and case.answer.error_code is None
    assert len(case.calls) == 1 and case.prechecks == [] and case.analysis_model.seen == []
    assert case.box.report["kind"] == "lookup"
    assert case.box.report["summary"] == case.answer.explanation
    assert case.answer.explanation_source == source
    assert len(case.persisted) == 1
    assert case.persisted[0]["explanation"] == case.answer.explanation
    assert case.persisted[0]["source"] == source
    assert case.persisted[0]["steps"] == case.answer.steps
    final_step = case.answer.steps[-1]
    assert final_step["node"] == "explain" and final_step["source"] == source and final_step["at"]


def test_lookup_generates_model_explanation_before_finishing_and_keeps_request_language(monkeypatch):
    text = "The governed result is seven.\n- One result was returned."
    case = setup_lookup(monkeypatch, [AIMessage(text)])
    prior_step = {"node": "govern", "result": "answered"}
    case.answer.steps = [prior_step]

    result = agent.lookup(case.state, case.config)

    assert result["phase"] == "done" and case.answer.explanation == text
    assert_answered_lookup(case, "model")
    assert case.answer.steps[0] == prior_step
    case.policy.assert_called_once_with(case.box.session)
    case.resolve.assert_called_once_with(case.box.session, "explain", temperature=0.2)
    case.model.invoke.assert_called_once()
    prompt = case.model.invoke.call_args.args[0][1].content
    assert prompt.endswith(reply_language_instruction("en"))
    assert case.box.budget.used_queries == 1


@pytest.mark.parametrize("send,configured_model", [(True, False), (False, True)])
def test_lookup_without_an_allowed_explain_model_keeps_the_answer(monkeypatch, send, configured_model):
    case = setup_lookup(monkeypatch, send=send, configured_model=configured_model)

    result = agent.lookup(case.state, case.config)

    assert result["phase"] == "done" and case.answer.explanation == case.answer.message
    assert_answered_lookup(case, "template")
    case.model.invoke.assert_not_called()
    case.model.bind_tools.assert_not_called()
    if send:
        case.resolve.assert_called_once_with(case.box.session, "explain", temperature=0.2)
    else:
        case.resolve.assert_not_called()


@pytest.mark.parametrize("failure_stage", ["resolve", "first_round", "second_round"])
def test_lookup_model_failure_records_a_redacted_step_and_keeps_template(monkeypatch, failure_stage):
    error = RuntimeError("upstream credential must not be saved")
    replies = [AIMessage(LEAKED_TOOL_MARKUP), error] if failure_stage == "second_round" else [error]
    case = setup_lookup(monkeypatch, replies)
    if failure_stage == "resolve":
        case.resolve.side_effect = error

    result = agent.lookup(case.state, case.config)

    assert result["phase"] == "done" and case.answer.explanation == case.answer.message
    assert_answered_lookup(case, "template")
    assert case.model.invoke.call_count == {"resolve": 0, "first_round": 1, "second_round": 2}[failure_stage]
    assert any(step.get("node") == "explain" and step.get("error") == "RuntimeError" for step in case.answer.steps)
    assert str(error) not in str(case.answer.steps)
    assert "chart" not in case.answer.evidence


@pytest.mark.parametrize("first_round", ["chart", "leaked_markup"])
def test_lookup_reuses_chart_and_protocol_retry_behavior(monkeypatch, first_round):
    choice = {"kind": "bar", "title": "成员指标"}
    first = AIMessage("", tool_calls=[{"name": "chart", "id": "choice", "args": choice}])
    if first_round == "leaked_markup":
        first.content = LEAKED_TOOL_MARKUP
    case = setup_lookup(monkeypatch, [first, AIMessage("甲高于乙。")], tools=True)
    case.answer.result = {
        "columns": ["member", "value"],
        "rows": [["甲", 7], ["乙", 3]],
        "column_roles": {
            "member": {"role": "dimension", "label": "成员"},
            "value": {"role": "measure", "label": "指标甲"},
        },
        "units": {"value": {"kind": "count", "additive": True}},
    }

    result = agent.lookup(case.state, case.config)

    assert result["phase"] == "done" and case.answer.explanation == "甲高于乙。"
    assert_answered_lookup(case, "model")
    assert case.model.invoke.call_count == 2
    case.model.bind_tools.assert_called_once()
    case.model.bind_tools.return_value.invoke.assert_called_once()
    retry_messages = case.model.invoke.call_args.args[0]
    if first_round == "chart":
        expected = ask_agent.ChartArgs.model_validate(choice).model_dump()
        assert case.answer.evidence["chart"] == case.persisted[0]["evidence"]["chart"] == expected
        assert retry_messages[2] is first and isinstance(retry_messages[3], ToolMessage)
        assert retry_messages[3].tool_call_id == "choice"
    else:
        assert "chart" not in case.answer.evidence
        assert {"node": "explain", "protocol": "leaked_tool_markup"} in case.answer.steps
        assert len(retry_messages) == 3 and isinstance(retry_messages[-1], HumanMessage)
        assert LEAKED_TOOL_MARKUP not in str(retry_messages)
