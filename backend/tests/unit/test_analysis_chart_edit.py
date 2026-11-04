"""续问改图：真实分析图与 chart 工具，内存数据集和按脚本回复的模型。"""

from __future__ import annotations

import json
import re
from copy import deepcopy

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from avelis import charts
from avelis.analysis import agent, conversation_context, dataset_access
from avelis.analysis.figures import dataset_chart_args
from avelis.analysis.tools import PermissionLost
from avelis.ask import service as ask_service
from avelis.models import AnalysisRun
from tests.unit.test_analysis_hitl import reply
from tests.unit.test_analysis_pipeline import WINDOW, Phased
from tests.unit.test_analysis_toolbox import make_box


class ChartModel(Phased):
    """记录每次绑定的工具，防止改图阶段顺带开放查询或报告工具。"""

    def __init__(self, responses):
        super().__init__(responses)
        self.bound_tools = []

    def bind_tools(self, tools, **kwargs):
        self.bound_tools.append(list(tools))
        return super().bind_tools(tools, **kwargs)


def configured(monkeypatch, question, changes, *, scope=None, targets=None):
    box = make_box(monkeypatch=monkeypatch)
    box.run.window, box.run.question = dict(WINDOW), question
    box.run.trigger, box.run.confirmation_mode = "manual", "auto"
    box._dataset(
        "calc",
        "地区",
        ["region", "amount"],
        [["甲", 10], ["乙", 30], ["丙", 20]],
        meta={
            "copied_from": {"run_id": "previous", "ref": "D1"},
            "units": {"amount": {"kind": "level", "unit": "元", "additive": True}},
            "column_labels": {"region": "地区", "amount": "金额"},
        },
    )
    targets = targets or [{"dataset": "D1", "title": "地区", "preference": {"kind": "bar", "title": "地区"}}]
    history = {"chart_targets": targets}
    # 本文件验证路由与工具；历史来源复制及刷新另有真实会话测试覆盖。
    monkeypatch.setattr(conversation_context, "refresh_followup_context", lambda *args: deepcopy(history))
    monkeypatch.setattr(conversation_context, "load_followup_datasets", lambda *args: None)
    monkeypatch.setattr(dataset_access, "can_read_dataset", lambda *args: True)
    monkeypatch.setattr(box, "precheck", lambda *args: pytest.fail("改图不应运行取数预检"))
    monkeypatch.setattr(box, "query_metric", lambda **kwargs: pytest.fail("改图不应查询指标"))
    monkeypatch.setattr(box, "query_sql", lambda **kwargs: pytest.fail("改图不应执行 SQL"))
    monkeypatch.setattr(ask_service, "fulfil", lambda *args, **kwargs: pytest.fail("改图不应调用问数执行器"))
    responses = [reply("submit_scope", scope or {"question_type": "chart"}, "scope")]
    if changes is not None:
        responses.append(reply("chart", changes, "chart"))
    model = ChartModel(responses)
    config = {"configurable": {"thread_id": "analysis:chart-edit", "toolbox": box, "model": model}}
    state = agent.initial_state(
        question,
        "2026-11-04",
        "UTC",
        WINDOW,
        inherited={"previous_question": "比较各地区金额", "conversation": history},
    )
    return box, model, config, state


@pytest.mark.parametrize(
    "question,changes",
    [
        ("画成饼图", {"kind": "donut"}),
        ("标出最高点", {"marks": ["max"]}),
        ("突出甲地区", {"highlight": ["甲"]}),
        ("只看前两项", {"top_n": 2}),
    ],
)
def test_followup_edits_existing_chart_without_queries_or_dataset_mutation(monkeypatch, question, changes):
    box, model, config, state = configured(monkeypatch, question, {"dataset": "D1", **changes})
    ds = box.datasets()["D1"]
    original = deepcopy((ds.columns, ds.rows, ds.meta, ds.inputs, ds.title))

    final = agent.build_graph(MemorySaver()).invoke(state, config)

    assert final["phase"] == "done" and not final.get("__interrupt__")
    assert box.report["kind"] == "chart" and box.report["dataset"] == "D1"
    preference = box.report["preference"]
    assert preference["kind"] == changes.get("kind", "bar") and preference["title"] == "地区"
    assert all(preference[key] == value for key, value in changes.items())
    assert box.report["figures"] == [{"dataset": "D1", **preference}]
    assert not re.search(r"\d", box.report["summary"])
    assert len(model.seen) == box.budget.used_model_requests == 2  # 理解一次，改图一次。
    assert [[tool.name for tool in tools] for tools in model.bound_tools] == [["submit_scope"], ["chart"]]
    assert box.budget.used_queries == 0 and list(box.datasets()) == ["D1"]
    ds = box.datasets()["D1"]
    assert (ds.columns, ds.rows, ds.meta, ds.inputs, ds.title) == original
    assert not any(getattr(step, "tool", None) in {"ask", "query_metric", "query_sql"} for step in box.session.added)
    text = "\n".join(str(message.content) for message in model.seen[1])
    assert question in text and all(value in text for value in ("地区", "金额", "level", "bar", "donut", "甲"))


@pytest.mark.parametrize(
    "question,changes,expected",
    [
        ("画成折线图", {"kind": "line"}, ["line", "bar", "donut"]),
        ("突出未登记的地区", {"highlight": ["未登记"]}, ["未登记", "甲", "乙", "丙"]),
    ],
)
def test_unavailable_chart_edit_explains_legal_options_without_fabricating_figure(
    monkeypatch, question, changes, expected
):
    box, model, config, state = configured(monkeypatch, question, {"dataset": "D1", **changes})

    final = agent.build_graph(MemorySaver()).invoke(state, config)

    assert final["phase"] == "done" and box.report["kind"] == "chart"
    assert box.report.get("figures", []) == [] and box.figures == []
    assert all(value in box.report["summary"] for value in expected)
    assert len(model.seen) == 2 and box.budget.used_queries == 0


def test_followup_needing_new_data_keeps_lookup_route(monkeypatch):
    box, model, config, state = configured(
        monkeypatch,
        "重新查询下个月各地区金额并画饼图",
        None,
        scope={"question_type": "lookup"},
    )
    lookups = []
    monkeypatch.setattr(agent, "lookup", lambda state, config: lookups.append(state["scope"]) or {"phase": "done"})
    monkeypatch.setattr(agent, "chart_edit", lambda *args: pytest.fail("新数据请求不应走改图分支"))

    final = agent.build_graph(MemorySaver()).invoke(state, config)

    assert final["phase"] == "done" and lookups[0]["question_type"] == "lookup"
    assert len(model.seen) == 1 and box.figures == []


def test_ambiguous_chart_target_asks_for_selection_before_edit_model_call(monkeypatch):
    targets = [
        {"dataset": "D1", "title": "地区", "preference": {"kind": "bar", "title": "地区"}},
        {"dataset": "D2", "title": "渠道", "preference": {"kind": "bar", "title": "渠道"}},
    ]
    box, model, config, state = configured(monkeypatch, "画成饼图", None, targets=targets)

    final = agent.build_graph(MemorySaver()).invoke(state, config)

    assert final["__interrupt__"][0].value["kind"] == "clarify"
    question = final["__interrupt__"][0].value["question"]
    assert "地区" in question and "渠道" in question
    assert len(model.seen) == 1 and box.figures == [] and box.budget.used_queries == 0


def test_explicit_chart_target_uses_named_figure_when_multiple_are_available(monkeypatch):
    targets = [
        {"dataset": "D1", "title": "地区", "preference": {"kind": "bar", "title": "地区"}},
        {"dataset": "D2", "title": "渠道", "preference": {"kind": "bar", "title": "渠道"}},
    ]
    box, model, config, state = configured(
        monkeypatch,
        "把渠道图画成饼图",
        {"dataset": "D2", "kind": "donut"},
        scope={"question_type": "chart", "chart_dataset": "D2"},
        targets=targets,
    )
    box._dataset(
        "calc",
        "渠道",
        ["channel", "amount"],
        [["直营", 10], ["合作", 30]],
        meta={"units": {"amount": {"kind": "level", "unit": "元", "additive": True}}},
    )

    final = agent.build_graph(MemorySaver()).invoke(state, config)

    assert final["phase"] == "done" and box.report["dataset"] == "D2"
    assert box.report["preference"]["kind"] == "donut" and box.report["preference"]["title"] == "渠道"
    assert len(model.seen) == 2 and box.budget.used_queries == 0


def test_revocation_between_understand_and_chart_edit_stops_before_second_model_call(monkeypatch):
    box, model, config, state = configured(monkeypatch, "标出最高点", {"dataset": "D1", "marks": ["max"]})
    invoke = model.invoke
    permissions = {"revoked": False, "checked": []}

    def can_read(session, user, dataset, known=None):
        permissions["checked"].append((f"D{dataset.seq}", permissions["revoked"]))
        return not permissions["revoked"]

    monkeypatch.setattr(dataset_access, "can_read_dataset", can_read)

    def revoke_after_understand(messages):
        result = invoke(messages)
        permissions["revoked"] = True
        return result

    monkeypatch.setattr(model, "invoke", revoke_after_understand)

    with pytest.raises(PermissionLost):
        agent.build_graph(MemorySaver()).invoke(state, config)

    assert len(model.seen) == 1 and box.report is None and box.figures == []
    assert box.budget.used_queries == 0
    assert ("D1", True) in permissions["checked"]


def test_confirmed_followup_scope_is_reunderstood_using_current_user_text(monkeypatch):
    user_text = "把上一轮图表画成饼图"
    box, model, config, state = configured(monkeypatch, "解释各地区金额为什么变化", {"kind": "donut"})
    state["inherited"].update(
        user_text=user_text,
        scope={"question_type": "change", "metric_codes": ["old_metric"], "dimensions": ["old_dimension"]},
    )

    final = agent.build_graph(MemorySaver()).invoke(state, config)

    assert final["phase"] == "done" and box.report["kind"] == "chart"
    assert final["scope"]["question_type"] == box.scope.question_type == "chart"
    assert final["scope"]["chart_dataset"] == box.report["dataset"] == "D1"
    assert final["scope"]["metric_codes"] == [] and final["scope"]["dimensions"] == []
    assert "scope" not in final["inherited"]
    assert box.report["preference"]["kind"] == "donut"
    assert len(model.seen) == 2 and box.budget.used_queries == 0
    assert all(user_text in "\n".join(str(message.content) for message in messages) for messages in model.seen)


@pytest.mark.parametrize("with_chart", [False, True])
def test_unexpected_tools_in_chart_reply_are_all_rejected_before_dispatch(monkeypatch, with_chart):
    box, model, config, state = configured(monkeypatch, "标出最高点", None)
    response = reply("query_metric", {"metric_codes": ["m_a"]}, "query")
    if with_chart:
        response.tool_calls.insert(0, {"name": "chart", "args": {"dataset": "D1", "marks": ["max"]}, "id": "chart"})
    model.replies.append(response)
    monkeypatch.setattr(box, "dispatch", lambda *args, **kwargs: pytest.fail("非法改图回复中的所有工具均应拒绝执行"))

    final = agent.build_graph(MemorySaver()).invoke(state, config)

    assert final["phase"] == "done" and box.report["kind"] == "chart"
    assert box.report["figures"] == [] and box.figures == [] and box.report["preference"] == {}
    assert box.verification == {"ok": False, "status": "chart"}
    assert "唯一且有效的 chart 调用" in box.report["summary"]
    assert len(model.seen) == 2 and box.budget.used_queries == 0
    denied = [step for step in box.session.added if getattr(step, "tool", None) in {"chart", "query_metric"}]
    assert [step.tool for step in denied] == [call["name"] for call in response.tool_calls]
    assert all(step.status == "denied" for step in denied)
    assert box.budget.used_calls == box.budget.used_tools == len(response.tool_calls)


def test_omitted_previous_preference_is_restored_before_applying_chart_patch(monkeypatch):
    target = {"dataset": "D1", "title": "地区", "preference": {}, "preference_omitted": True}
    box, model, config, state = configured(monkeypatch, "标出最高点", {"marks": ["max"]}, targets=[target])
    box.datasets()["D1"].rows = [["甲", 30], ["乙", 20]]  # 确保上一轮高亮的甲属于所示类目。
    parent = AnalysisRun(id="previous")
    box.run.parent_run_id = parent.id
    box.session.store[(AnalysisRun, parent.id)] = parent
    previous = {"kind": "bar", "title": "地区", "sort": "category", "highlight": ["甲"], "top_n": 2}
    recovered = []

    def complete_targets(session, user, run, datasets):
        recovered.append((session, user, run, datasets))
        return [{"dataset": "D1", "title": "地区", "preference": dict(previous)}]

    monkeypatch.setattr(conversation_context, "_chart_targets", complete_targets)

    final = agent.build_graph(MemorySaver()).invoke(state, config)

    assert final["phase"] == "done" and box.report["kind"] == "chart"
    assert box.report["preference"] == {**previous, "marks": ["max"]}, box.report["summary"]
    assert box.report["figures"] == [{"dataset": "D1", **previous, "marks": ["max"]}]
    assert recovered == [(box.session, box.user, parent, box.datasets())]
    assert len(model.seen) == 2 and box.budget.used_queries == 0
    assert [[tool.name for tool in tools] for tools in model.bound_tools] == [["submit_scope"], ["chart"]]


def test_ambiguous_chart_selection_resumes_with_clarification_and_edits_only_selected_dataset(monkeypatch):
    targets = [
        {"dataset": "D1", "title": "地区", "preference": {"kind": "bar", "title": "地区"}},
        {"dataset": "D2", "title": "渠道", "preference": {"kind": "bar", "title": "渠道"}},
    ]
    box, model, config, state = configured(monkeypatch, "标出最高点", None, targets=targets)
    box._dataset(
        "calc",
        "渠道",
        ["channel", "amount"],
        [["直营", 10], ["合作", 30]],
        meta={"units": {"amount": {"kind": "level", "unit": "元", "additive": True}}},
    )
    model.replies.extend(
        [
            reply("submit_scope", {"question_type": "chart", "chart_dataset": "D2"}, "selected"),
            reply("chart", {"marks": ["max"]}, "chart"),
        ]
    )
    graph = agent.build_graph(MemorySaver())

    first = graph.invoke(state, config)
    question = first["__interrupt__"][0].value["question"]
    final = graph.invoke(Command(resume={"answer": "渠道那张"}), config)

    assert final["phase"] == "done" and not final.get("__interrupt__")
    assert final["clarifications"] == [{"question": question, "answer": "渠道那张"}]
    assert box.report["dataset"] == "D2"
    assert box.report["figures"] == [{"dataset": "D2", "kind": "bar", "title": "渠道", "top_n": None, "marks": ["max"]}]
    assert all(figure["dataset"] == "D2" for figure in box.figures)
    assert "渠道那张" in "\n".join(str(message.content) for message in model.seen[-1])
    assert len(model.seen) == 3 and box.budget.used_queries == 0


def test_chart_model_budget_cutoff_reports_budget_reason_instead_of_invalid_call(monkeypatch):
    box, model, config, state = configured(monkeypatch, "标出最高点", None)
    box.budget.output_tokens = 100000
    invoke = agent._invoke
    calls = []

    def cutoff_second_request(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            box.budget._blocked_output_tokens = True
            return None
        return invoke(*args, **kwargs)

    monkeypatch.setattr(agent, "_invoke", cutoff_second_request)

    final = agent.build_graph(MemorySaver()).invoke(state, config)

    assert final["phase"] == "done" and box.report["kind"] == "chart"
    assert box.budget.model_blocked() in box.report["summary"]
    assert "没有提交唯一" not in box.report["summary"]
    assert box.report["figures"] == [] and box.figures == []
    assert box.verification == {"ok": False, "status": "chart"}
    assert len(calls) == 2 and len(model.seen) == 1 and box.budget.used_queries == 0


def test_chart_context_lists_current_multi_measure_subset_and_keeps_it_when_adding_marks(monkeypatch):
    previous = {"kind": "grouped_bar", "title": "地区金额对比", "measures": ["a", "b"]}
    target = {"dataset": "D1", "title": "地区金额对比", "preference": previous}
    box, model, config, state = configured(monkeypatch, "标出最高点", {"marks": ["max"]}, targets=[target])
    dataset = box.datasets()["D1"]
    dataset.columns = ["region", "a", "b", "c"]
    dataset.rows = [["甲", 10, 20, 3], ["乙", 30, 15, 5]]
    dataset.meta = {
        "units": {
            "a": {"kind": "level", "unit": "USD", "additive": True},
            "b": {"kind": "level", "unit": "USD", "additive": True},
            "c": {"kind": "level", "unit": "kg", "additive": True},
        }
    }

    final = agent.build_graph(MemorySaver()).invoke(state, config)

    context, _ = json.JSONDecoder().raw_decode(model.seen[1][1].content)
    choices = [choice for choice in context["available_chart_options"] if choice["measures"] == ["a", "b"]]
    assert len(choices) == 1 and "grouped_bar" in choices[0]["kinds"]
    assert final["phase"] == "done" and box.report["kind"] == "chart"
    assert all(box.report["preference"][key] == value for key, value in previous.items())
    assert box.report["preference"]["marks"] == ["max"]
    assert len(model.seen) == 2 and box.budget.used_queries == 0


def test_source_question_title_is_not_saved_as_chart_title_when_previous_preference_is_empty(monkeypatch):
    source_title = "2026 年请比较各地区金额与数量，并展示每个地区的完整结果、期间范围以及所使用的口径和筛选条件"
    target = {"dataset": "D1", "title": source_title, "preference": {}}
    box, model, config, state = configured(monkeypatch, "标出最高点", {"marks": ["max"]}, targets=[target])

    final = agent.build_graph(MemorySaver()).invoke(state, config)

    context, _ = json.JSONDecoder().raw_decode(model.seen[1][1].content)
    assert context["current_title"] == source_title
    assert final["phase"] == "done" and box.verification == {"ok": True, "status": "chart"}
    assert box.report["preference"]["title"] != source_title
    assert len(box.report["preference"]["title"]) <= 40 and not re.search(r"\d", box.report["preference"]["title"])
    assert box.report["preference"]["marks"] == ["max"] and box.report["figures"]
    assert len(model.seen) == 2 and box.budget.used_queries == 0


def test_chart_context_includes_low_value_category_available_after_sorting_ascending(monkeypatch):
    box, model, config, state = configured(
        monkeypatch,
        "按金额升序并突出 item1",
        {"sort": "value_asc", "highlight": ["item1"]},
    )
    dataset = box.datasets()["D1"]
    dataset.rows = [[f"item{value}", value] for value in range(1, 41)]
    dataset.meta = {"units": {"amount": {"kind": "level", "unit": "元", "additive": False}}}
    default = charts.recommend(
        **dataset_chart_args({"columns": dataset.columns, "rows": dataset.rows, "meta": dataset.meta})
    )
    assert "item1" not in default[0]["categories"]

    final = agent.build_graph(MemorySaver()).invoke(state, config)

    context, _ = json.JSONDecoder().raw_decode(model.seen[1][1].content)
    assert "item1" in context["available_categories"]
    assert final["phase"] == "done" and box.verification == {"ok": True, "status": "chart"}
    assert box.report["preference"]["sort"] == "value_asc"
    assert box.report["preference"]["highlight"] == ["item1"] and box.report["figures"]
    assert len(model.seen) == 2 and box.budget.used_queries == 0


def test_grouped_bar_context_materializes_ascending_categories_with_the_actual_chart_kind(monkeypatch):
    target = {"dataset": "D1", "title": "地区对比", "preference": {"kind": "grouped_bar", "title": "地区对比"}}
    box, model, config, state = configured(
        monkeypatch,
        "按金额升序并突出 item1",
        {"sort": "value_asc", "highlight": ["item1"]},
        targets=[target],
    )
    dataset = box.datasets()["D1"]
    dataset.columns = ["region", "a", "b"]
    dataset.rows = [[f"item{value}", value, value * 2] for value in range(1, 41)]
    dataset.meta = {
        "units": {
            "a": {"kind": "level", "unit": "USD", "additive": False},
            "b": {"kind": "level", "unit": "USD", "additive": False},
        }
    }
    default = charts.recommend(
        **dataset_chart_args({"columns": dataset.columns, "rows": dataset.rows, "meta": dataset.meta})
    )
    assert default[0]["kind"] == "grouped_bar" and "item1" not in default[0]["categories"]
    materialize = charts.ChartPreference.materialize
    grouped_ascending = []

    def remember_grouped_ascending(preference, *args, **kwargs):
        result = materialize(preference, *args, **kwargs)
        if preference.measures is None and preference.kind == "grouped_bar" and preference.sort == "value_asc":
            grouped_ascending.append(deepcopy(result))
        return result

    monkeypatch.setattr(charts.ChartPreference, "materialize", remember_grouped_ascending)

    final = agent.build_graph(MemorySaver()).invoke(state, config)

    context, _ = json.JSONDecoder().raw_decode(model.seen[1][1].content)
    assert grouped_ascending and all("item1" in option["categories"] for option in grouped_ascending)
    assert "item1" in context["available_categories"]
    assert final["phase"] == "done" and box.verification == {"ok": True, "status": "chart"}
    assert box.report["preference"]["kind"] == "grouped_bar" and box.report["preference"]["sort"] == "value_asc"
    assert box.report["preference"]["highlight"] == ["item1"] and box.report["figures"]
    assert len(model.seen) == 2 and box.budget.used_queries == 0


def test_confirmed_followup_that_is_not_a_chart_edit_keeps_the_confirmed_scope(monkeypatch):
    """理解阶段只用来判断是否改图；判为其他类型时沿用用户确认过的范围，不被模型改写。"""
    confirmed = {"question_type": "change", "metric_codes": ["old_metric"], "dimensions": ["old_dimension"]}
    box, model, config, state = configured(
        monkeypatch,
        "解释各地区金额为什么变化",
        None,
        scope={"question_type": "lookup", "metric_codes": ["other_metric"]},
    )
    state["inherited"].update(user_text="再看看华东", scope=confirmed)
    result = agent.understand(state, config)
    assert result["phase"] == "precheck"
    assert result["scope"]["question_type"] == "change"
    assert result["scope"]["metric_codes"] == ["old_metric"] and result["scope"]["dimensions"] == ["old_dimension"]
    assert result["inherited"]["scope"] == confirmed
    assert len(model.seen) == 1
