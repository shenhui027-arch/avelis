"""第二阶段问数：模型替身与内存对象，不连接数据库或外部模型。"""

import json
import logging
from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import psycopg
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy.exc import OperationalError

from avelis import config, modelcfg
from avelis.analysis import grounding
from avelis.api.ask import _answer_result
from avelis.ask import agent, service
from avelis.ask.intent import DemoIntentModel, Intent, MetricCard
from avelis.charts import recommend
from avelis.i18n import locale_context
from avelis.llm_protocol import ReasoningRoundTripChatOpenAI
from avelis.models import AskThread, Domain, ModelProvider


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    settings = config.build_settings({"AVELIS_SECRET": "test-secret-0123456789abcdef0123456789"})
    for module in (agent, service, grounding):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    return settings


def answer_for(rows=None):
    return SimpleNamespace(
        status="answered",
        message="模板解释",
        question="各区域成交额",
        intent={"start_date": "2026-09-01", "end_date": "2026-09-30"},
        evidence={
            "metric_version": {
                "metric_name": "成交额",
                "description": "已支付订单总额",
                "metric_code": "secret_code",
                "version_no": 7,
                "dependency_fingerprint": "secret_fingerprint",
                "owner": "secret_owner",
                "reviewed_by": "secret_reviewer",
                "published_at": "secret_date",
                "expression": "secret_expression",
            }
        },
        result={
            "columns": ["region_code", "amount", "rate"],
            "rows": rows
            if rows is not None
            else [["华东", Decimal("6729876.54321"), Decimal("0.06612345")], ["华北", 12, None]],
            "units": {"amount": {"kind": "level", "unit": "元"}, "rate": {"kind": "ratio"}},
            "column_roles": {
                "region_code": {"role": "dimension", "label": "区域"},
                "amount": {"role": "measure", "label": "成交额"},
                "rate": {"role": "measure", "label": "转化率"},
            },
        },
    )


def explain(monkeypatch, answer, reply=None, *, support=True, send=True, configured=True):
    model = Mock()
    if isinstance(reply, list):
        model.invoke.side_effect = reply
    else:
        model.invoke.return_value = reply if reply is not None else AIMessage("结果解释")
    model.bind_tools.return_value = Mock(invoke=Mock(side_effect=model.invoke))
    assignment = SimpleNamespace(provider_id="provider", model_id="model")
    provider = SimpleNamespace(last_check={"models": {"model": {"tool_calling": support}}})
    session = Mock()
    session.get.return_value = provider
    monkeypatch.setattr(modelcfg, "policy", lambda *_: {"send_results": send})
    resolve = Mock(return_value=SimpleNamespace(model=model) if configured else None)
    monkeypatch.setattr(modelcfg, "resolve", resolve)
    monkeypatch.setattr(modelcfg, "get_assignment", lambda *_: assignment)
    state = {"intent": answer.intent}
    outcome = agent.explain(state, {"configurable": {"session": session, "user": object(), "answer": answer}})
    if not send:
        resolve.assert_not_called()
    return model, outcome


def payload(model):
    return json.JSONDecoder().raw_decode(model.invoke.call_args.args[0][1].content)[0]


def test_explain_only_business_fields_and_display_values(monkeypatch):
    answer = answer_for()
    before = deepcopy(answer.result)
    model, _ = explain(monkeypatch, answer, support=False)
    sent = payload(model)
    assert set(sent) == {"question", "metric", "period", "columns", "rows", "chart_options"}
    assert sent["metric"] == {"name": "成交额", "description": "已支付订单总额"}
    assert sent["period"] == answer.intent
    assert sent["columns"] == ["区域", "成交额", "转化率"]
    assert sent["rows"] == [["华东", "672.99万元", "6.6%"], ["华北", "12元", None]]
    text = json.dumps(sent, ensure_ascii=False)
    assert all(value not in text for value in ("secret_", "6729876.54321", "0.06612345", "region_code", "version_no"))
    assert answer.result == before
    assert answer.explanation_source == "model"


@pytest.mark.parametrize("size", [20, 21, 30])
def test_explain_full_rows_or_extrema_and_temporal_summary(monkeypatch, size):
    answer = answer_for([[f"2026-09-{i:02}", i, Decimal("0.06612345")] for i in range(size, 0, -1)])
    answer.result["column_roles"]["region_code"] = {"role": "time", "label": "日期"}
    model, _ = explain(monkeypatch, answer, support=False)
    rows = payload(model)["rows"]
    if size == 20:
        assert len(rows) == 20
    else:
        assert rows == {
            "row_count": size,
            "max": [f"2026-09-{size:02}", f"{size}元", "6.6%"],
            "min": ["2026-09-01", "1元", "6.6%"],
            "first": ["2026-09-01", "1元", "6.6%"],
            "last": [f"2026-09-{size:02}", f"{size}元", "6.6%"],
        }


def test_summary_handles_null_values_and_missing_time(monkeypatch):
    answer = answer_for([[str(i), None, None] for i in range(21)])
    answer.evidence = {}
    model, _ = explain(monkeypatch, answer, support=False)
    assert payload(model)["rows"] == {"row_count": 21}
    assert payload(model)["metric"] == {}


@pytest.mark.parametrize("template", ["single", "plan"])
@pytest.mark.parametrize(
    "value,meta,expected",
    [
        ("91622215.01", {"kind": "level", "unit": "元"}, "9162.22万元"),
        ("1.2345", {"kind": "level", "unit": "元", "scale": "10000"}, "1.23万元"),
        ("0.06612345", {"kind": "ratio"}, "6.6%"),
        ("0.06612345", {"kind": "delta", "of_ratio": True}, "+6.6个百分点"),
        ("0", {"kind": "count", "unit": "单"}, "0单"),
        (None, {"kind": "level", "unit": "元"}, "无数据"),
        ("91622215.0100", {"kind": "unknown"}, "91622215.0100"),
        ("91622215.0100", {"kind": "unexpected"}, "91622215.0100"),
        ("91622215.0100", None, "91622215.0100"),
        ("文本原值", {"kind": "level", "unit": "元"}, "文本原值"),
    ],
)
def test_answer_templates_format_measures_without_changing_wording(template, value, meta, expected):
    metric, version = SimpleNamespace(name="成交额"), SimpleNamespace(version_no=1)
    intent = Intent(metric_code="amount", start_date=date(2026, 9, 1), end_date=date(2026, 9, 30))
    result = {
        "columns": ["成交额"],
        "rows": [[value]],
        "units": {"成交额": meta} if meta is not None else {},
        "column_roles": {"成交额": {"role": "measure"}},
    }
    before = deepcopy(result)
    with locale_context("zh-CN"):
        if template == "single":
            text = service._explain(metric, version, intent, result)
        else:
            text = service._explain_plan([("amount", metric, version)], intent, result)
            expected = "成交额 " + expected
    assert text == f"2026-09-01 至 2026-09-30 成交额（V1）：{expected}"
    assert result == before


@pytest.mark.parametrize(
    "style,expected",
    [
        ("cn", "成交额 672.99万元；成交额·上一期 734.93万元；成交额·变化 -61.94万元；成交额·变化率 -8.4%"),
        ("intl", "成交额 6.73M元；成交额·上一期 7.35M元；成交额·变化 -619.44K元；成交额·变化率 -8.4%"),
    ],
)
def test_comparison_template_uses_governed_units_and_number_style(monkeypatch, settings, style, expected):
    monkeypatch.setattr(grounding, "get_settings", lambda: replace(settings, number_style=style))
    metric = SimpleNamespace(name="成交额")
    version = SimpleNamespace(version_no=1, value_kind="level", unit="元", expression="SUM(amount)", dimensions=[])
    intent = Intent(
        metric_code="amount", start_date=date(2026, 9, 1), end_date=date(2026, 9, 30), compare="previous_period"
    )
    result = service._result_contract(
        {
            "columns": ["成交额", "成交额·上一期", "成交额·变化", "成交额·变化率"],
            "rows": [["6729908.70", "7349347.75", "-619439.05", "-0.0843"]],
        },
        intent,
        [(metric, version)],
    )
    with locale_context("zh-CN"):
        assert service._explain_plan([("amount", metric, version)], intent, result) == (
            "2026-09-01 至 2026-09-30 成交额（V1）：" + expected
        )


@pytest.mark.parametrize("role", ["dimension", "time"])
def test_template_preserves_numeric_dimension_and_time(role):
    metric, version = SimpleNamespace(name="成交额"), SimpleNamespace(version_no=1)
    intent = Intent(metric_code="amount", start_date=date(2026, 9, 1), end_date=date(2026, 9, 1))
    result = {
        "columns": ["编码", "成交额"],
        "rows": [["20260901", "12"]],
        "units": {"编码": {"kind": "count"}, "成交额": {"kind": "level", "unit": "元"}},
        "column_roles": {"编码": {"role": role}, "成交额": {"role": "measure"}},
    }
    with locale_context("zh-CN"):
        assert service._explain_plan([("amount", metric, version)], intent, result) == (
            "2026-09-01 成交额（V1）：编码 20260901；成交额 12元"
        )


def chart_reply(kind="bar", text="", **args):
    return AIMessage(
        text,
        tool_calls=[{"id": "selection", "name": "chart", "args": {"kind": kind, "title": "华东成交额最高", **args}}],
    )


def test_chart_choice_with_text_saved_and_model_invoked_once(monkeypatch):
    answer = answer_for()
    text = "华东成交额最高。"
    model, _ = explain(monkeypatch, answer, chart_reply(text=text, top_n=2))
    assert answer.evidence["chart"] == {"kind": "bar", "title": "华东成交额最高", "top_n": 2}
    assert answer.explanation == text
    assert answer.explanation_source == "model"
    assert model.invoke.call_count == 1
    tool = model.bind_tools.call_args.args[0][0]
    assert tool.name == "chart" and tool.args_schema is agent.ChartArgs
    assert tool.description == agent.CHART_DESCRIPTION
    assert payload(model)["chart_options"] == ["multiples", "bar"]


def test_chart_only_reply_gets_unbound_second_round_and_preserves_reasoning(monkeypatch):
    answer = answer_for()
    first = chart_reply(top_n=2)
    first.additional_kwargs["reasoning_content"] = "private reasoning"
    model, outcome = explain(monkeypatch, answer, [first, AIMessage("华东成交额最高。")])
    assert answer.explanation == "华东成交额最高。"
    assert answer.explanation_source == "model"
    assert answer.evidence["chart"] == {"kind": "bar", "title": "华东成交额最高", "top_n": 2}
    assert model.invoke.call_count == 2
    model.bind_tools.assert_called_once()
    model.bind_tools.return_value.invoke.assert_called_once()
    messages = model.invoke.call_args.args[0]
    assert messages[:2] == model.invoke.call_args_list[0].args[0]
    assert messages[2] is first
    assert isinstance(messages[3], ToolMessage)
    assert messages[3].tool_call_id == "selection"
    assert messages[3].content == "已按 bar 生成图：华东成交额最高"
    wire = ReasoningRoundTripChatOpenAI(model="deepseek", api_key="test")._get_request_payload(messages)
    assert "tools" not in wire
    assert wire["messages"][2]["reasoning_content"] == "private reasoning"
    assert wire["messages"][3]["tool_call_id"] == "selection"
    assert "private reasoning" not in json.dumps(outcome, default=str)


@pytest.mark.parametrize("second", [AIMessage(""), chart_reply("bar")])
def test_explain_stops_after_two_rounds_when_second_has_no_text(monkeypatch, second):
    answer = answer_for()
    model, _ = explain(monkeypatch, answer, [chart_reply(), second])
    assert model.invoke.call_count == 2
    assert answer.explanation == "模板解释"
    assert answer.explanation_source == "template"


def test_empty_reply_without_tool_does_not_request_second_round(monkeypatch):
    answer = answer_for()
    model, _ = explain(monkeypatch, answer, AIMessage(" \n "))
    assert model.invoke.call_count == 1
    assert answer.explanation_source == "template"


@pytest.mark.parametrize(
    "kind,args", [("line", {}), ("unknown", {}), ("bar", {"top_n": 21}), ("bar", {"title": "长" * 41})]
)
def test_invalid_chart_choice_returns_options_then_gets_explanation(monkeypatch, kind, args):
    answer = answer_for()
    model, outcome = explain(monkeypatch, answer, [chart_reply(kind, **args), AIMessage("华东成交额最高。")])
    assert "chart" not in answer.evidence
    assert {"node": "explain", "chart": "rejected", "kind": kind} in outcome["steps"]
    assert model.invoke.call_count == 2
    assert model.invoke.call_args.args[0][-1].content == "该图不可用，可选：multiples、bar"
    assert answer.explanation == "华东成交额最高。"


@pytest.mark.parametrize("support", [False, None])
def test_unverified_or_unsupported_model_does_not_bind_tools(monkeypatch, support):
    answer = answer_for()
    model, _ = explain(monkeypatch, answer, support=support)
    model.bind_tools.assert_not_called()
    assert answer.explanation == "结果解释"


@pytest.mark.parametrize("mode", ["kpi", "no_options", "demo", "private"])
def test_chart_is_not_bound_when_unavailable(monkeypatch, mode):
    answer = answer_for()
    if mode == "kpi":
        answer.result["rows"] = [["华东", 1, 0.1]]
    elif mode == "no_options":
        answer.result["column_roles"] = {}
    model, _ = explain(monkeypatch, answer, send=mode != "private", configured=mode != "demo")
    model.bind_tools.assert_not_called()
    if mode in ("private", "demo"):
        model.invoke.assert_not_called()
        assert answer.explanation_source == "template"


def preference_inputs(kind="level", size=30):
    return (
        ["d", "v"],
        [[str(i), i] for i in range(size)],
        {"d": {"role": "dimension"}, "v": {"role": "measure"}},
        {"v": {"kind": kind}},
        {},
    )


def test_preference_moves_chart_first_and_sets_only_selected_title():
    args = preference_inputs(size=5)
    before = deepcopy(args)
    default = recommend(*args)
    result = recommend(*args, preference={"kind": "donut", "title": "最大项为 4", "top_n": 2})
    assert result[0] == {**default[1], "title": "最大项为 4"}
    assert result[1] == default[0] and result[1]["title"] is None
    assert args == before


@pytest.mark.parametrize("kind,other", [("level", True), ("ratio", False)])
def test_preference_top_n_merges_only_additive_tail(kind, other):
    result = recommend(*preference_inputs(kind), preference={"kind": "bar", "top_n": 2})[0]
    assert result["categories"][:2] == ["29", "28"]
    assert result["categories"][2:] == (["其他"] if other else [])
    assert result["others"] == ({"count": 28} if other else None)
    assert result["series"][0]["values"] == ([29, 28, sum(range(28))] if other else [29, 28])
    capped = recommend(*preference_inputs(kind), preference={"kind": "bar", "top_n": 20})[0]
    assert len(capped["categories"]) == (21 if other else 20)


@pytest.mark.parametrize(
    "preference",
    [
        None,
        [],
        {"kind": "line"},
        {"kind": "kpi"},
        {"kind": "bar", "top_n": 1},
        {"kind": "bar", "top_n": True},
        {"kind": "bar", "top_n": "2"},
        {"kind": "bar", "title": 42},
        {"kind": "bar", "title": "长" * 41},
        {"kind": "bar", "extra": 1},
    ],
)
def test_invalid_preference_is_ignored_as_a_whole(preference):
    args = preference_inputs()
    assert recommend(*args, preference=preference) == recommend(*args)


def test_api_reads_preference_without_mutating_result():
    answer = answer_for()
    answer.evidence["chart"] = {"kind": "bar", "title": "华东最高", "top_n": 2}
    before = deepcopy(answer.result)
    assert _answer_result(answer, None)["charts"][0]["title"] == "华东最高"
    assert answer.result == before


@pytest.mark.parametrize("kind", ["grouped_bar", "multiples", "bar"])
def test_multi_measure_top_n_aggregates_only_additive_displayed_series(kind):
    columns, rows, roles, units, polarity = preference_inputs(size=30)
    columns.append("second")
    roles["second"] = {"role": "measure"}
    units["second"] = {"kind": "level" if kind == "grouped_bar" else "ratio"}
    rows = [[*row, row[1] * 2] for row in rows]
    args = (columns, rows, roles, units, polarity)
    default = recommend(*args)
    result = recommend(*args, preference={"kind": kind, "top_n": 2})
    chosen = result[0]
    assert chosen["kind"] == kind
    additive = kind != "multiples"
    assert chosen["categories"] == ["29", "28", *(["其他"] if additive else [])]
    assert chosen["others"] == ({"count": 28} if additive else None)
    if additive:
        assert chosen["series"][0]["values"] == [29, 28, sum(range(28))]
    for candidate in result[1:]:
        assert candidate == next(s for s in default if s["kind"] == candidate["kind"])


@pytest.mark.parametrize("kind", ["diverging_bar", "grouped_bar"])
@pytest.mark.parametrize("unit_kind", ["level", "ratio"])
def test_comparison_top_n_merges_tail_only_when_additive(kind, unit_kind):
    columns = ["d", "value", "销售额·上一期", "销售额·变化", "销售额·变化率"]
    roles = {
        c: {"role": role}
        for c, role in zip(columns, ["dimension", "measure", "measure", "change", "rate"], strict=True)
    }
    units = {
        c: {"kind": unit_kind if role["role"] == "measure" else "delta" if unit_kind == "level" else "ratio"}
        for c, role in roles.items()
    }
    rows = [[str(i), i, i * 2, -i, -0.5] for i in range(30)]
    args = (columns, rows, roles, units, {})
    default = recommend(*args)
    chosen, other = recommend(*args, preference={"kind": kind, "title": "对比结果", "top_n": 2})
    assert chosen["kind"] == kind and chosen["title"] == "对比结果"
    assert chosen["categories"][:2] == (["0", "1"] if kind == "diverging_bar" else ["29", "28"])
    assert chosen["categories"][2:] == (["其他"] if unit_kind == "level" else [])
    assert chosen["others"] == ({"count": 28} if unit_kind == "level" else None)
    if unit_kind == "level":
        assert chosen["series"][0]["values"][-1] == (-sum(range(2, 30)) if kind == "diverging_bar" else sum(range(28)))
    assert other == next(s for s in default if s["kind"] == other["kind"])


def test_time_chart_ignores_top_n():
    columns, rows, roles, units, polarity = preference_inputs(size=30)
    roles["d"] = {"role": "time"}
    args = (columns, rows, roles, units, polarity)
    default = recommend(*args)
    result = recommend(*args, preference={"kind": "bar", "title": "时间趋势", "top_n": 2})
    assert result[0] == {**default[1], "title": "时间趋势"}
    assert result[1] == default[0]


def test_only_first_chart_call_is_processed(monkeypatch):
    answer = answer_for()
    reply = chart_reply("line")
    reply.tool_calls.extend(chart_reply("bar").tool_calls)
    reply.tool_calls[1]["id"] = "extra-selection"
    model, outcome = explain(monkeypatch, answer, [reply, AIMessage("结果解释")])
    assert "chart" not in answer.evidence
    assert {"node": "explain", "chart": "rejected", "kind": "line"} in outcome["steps"]
    assert model.invoke.call_count == 2
    assert [message.tool_call_id for message in model.invoke.call_args.args[0][3:]] == [
        "selection",
        "extra-selection",
    ]


@pytest.mark.parametrize(
    "error",
    [psycopg.OperationalError("offline"), OperationalError("query", {}, Exception("offline")), RuntimeError("offline")],
)
@pytest.mark.parametrize(
    "locale,db_text,model_text",
    [
        ("zh-CN", "查询数据源失败：", "模型调用失败："),
        ("zh-TW", "查詢資料來源失敗：", "模型呼叫失敗："),
        ("en", "Data source query failed: ", "Model invocation failed: "),
    ],
)
def test_run_turn_logs_traceback_and_distinguishes_database_errors(
    monkeypatch, caplog, error, locale, db_text, model_text
):
    thread = SimpleNamespace(id="thread", user_id="user", status="active")
    session = Mock()
    session.get.return_value = thread
    answer = SimpleNamespace()
    monkeypatch.setattr(modelcfg, "resolve", lambda *_: None)
    monkeypatch.setattr(service, "start_answer", lambda *_: answer)
    monkeypatch.setattr(service, "finish_answer", lambda *args: args[2])
    graph = Mock()
    graph.get_state.return_value = SimpleNamespace(values={}, next=())
    graph.invoke.side_effect = error
    monkeypatch.setattr(agent, "graph", lambda: graph)
    with locale_context(locale), caplog.at_level(logging.ERROR, logger=agent.__name__):
        returned = agent.run_turn(session, SimpleNamespace(id="user"), "问题", thread.id)
    assert returned is answer and answer.status == "error"
    prefix = model_text if isinstance(error, RuntimeError) else db_text
    assert answer.message == prefix + type(error).__name__
    assert any(record.exc_info and record.exc_info[1] is error for record in caplog.records)
    session.get.assert_called_once_with(AskThread, "thread")


def duplicate_cards():
    return [
        MetricCard(code=code, name="订单数", aliases=["订单量"], dimensions=[], domain=domain)
        for code, domain in [("retail", "零售"), ("wholesale", "批发")]
    ]


@pytest.mark.parametrize("question", ["2026-09-01 订单数", "2026-09-01 订单量", "多少"])
def test_duplicate_candidates_include_domain_and_resolve_followup(question):
    cards = duplicate_cards()
    model = DemoIntentModel()
    intent = model.parse(question, cards, today=date(2026, 9, 1))
    assert intent.candidates == ["订单数（零售）", "订单数（批发）"]
    for card, label in zip(cards, intent.candidates, strict=True):
        parsed = model.parse(f"{question} {label}", cards, today=date(2026, 9, 1))
        assert parsed.metric_codes == [card.code]


def test_unique_metric_name_and_alias_are_unchanged():
    card = duplicate_cards()[0]
    model = DemoIntentModel()
    for label in (card.name, card.aliases[0]):
        assert model.parse(f"2026-09-01 {label}", [card], today=date(2026, 9, 1)).metric_code == card.code


def test_visible_catalog_and_search_tool_include_domain(monkeypatch):
    metric = SimpleNamespace(code="orders", name="订单数", aliases=[], ai_context={}, domain_id="domain")
    version = SimpleNamespace(dimensions=[], description="订单总数", status="published")
    session = Mock()
    session.get.return_value = SimpleNamespace(name="零售")
    monkeypatch.setattr(service.access, "require_role", lambda *_: None)
    monkeypatch.setattr(service, "visible_metrics", lambda *_: [metric])
    monkeypatch.setattr(service, "latest_governed_version", lambda *_: version)
    catalog = service.visible_catalog(session, object(), with_values=False)
    session.get.assert_called_once_with(Domain, "domain")
    assert catalog.cards[0].domain == "零售"
    assert json.loads(agent.Toolbox(session, object(), catalog).search_metrics())[0]["domain"] == "零售"


@pytest.mark.parametrize(
    "check,expected",
    [
        ({"models": {"model": {"tool_calling": True}}}, True),
        ({"model_id": "model", "tool_calling": True}, True),
        ({"models": {"other": {"tool_calling": True}}}, False),
        ({}, False),
    ],
)
def test_tool_support_uses_saved_model_specific_check(monkeypatch, check, expected):
    monkeypatch.setattr(modelcfg, "get_assignment", lambda *_: SimpleNamespace(provider_id="p", model_id="model"))
    session = Mock()
    session.get.return_value = SimpleNamespace(last_check=check)
    assert agent._explain_supports_tools(session) is expected
    session.get.assert_called_once_with(ModelProvider, "p")
