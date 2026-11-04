"""多项筛选一次确认、兼容旧回答和继承范围内的局部纠正。"""

from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from avelis.analysis import agent
from avelis.analysis.service import _clarification_answer
from avelis.analysis.tools import Scope
from avelis.ask.alignment import AlignmentResult
from avelis.ask.intent import DimensionFilter
from tests.unit.test_analysis_entry import configured, fake_ask, scope_reply


def multi_value_run(monkeypatch):
    box, model, config, state, _ = configured(
        monkeypatch,
        [scope_reply(filters=[{"dimension": "地区", "values": ["别名一", "别名二", "别名三"]}])],
    )
    calls = []
    choices = {"别名一": "成员一", "别名二": "成员二", "别名三": "成员三"}

    def align(session, user, version, filters, settings, *, remaining_seconds):
        calls.append(([item.model_dump() for item in filters], remaining_seconds()))
        return AlignmentResult(
            filters,
            unresolved=[
                {
                    "dimension": item.dimension,
                    "original": value,
                    "candidates": [choices[value]],
                    "has_dimension_table": False,
                }
                for item in filters
                for value in item.values
                if value in choices
            ],
        )

    monkeypatch.setattr(agent, "align_filters", align)
    monkeypatch.setattr(agent, "has_dimension_table", lambda *args: False)
    monkeypatch.setattr(agent.service, "version_sources", lambda *args: {})
    _, queries = fake_ask(monkeypatch, box)
    return box, model, config, state, calls, queries


def test_all_pending_items_can_be_selected_in_one_structured_answer(monkeypatch):
    box, model, config, state, calls, queries = multi_value_run(monkeypatch)
    # 已用完模型澄清额度也必须允许确定性候选确认。
    state["clarifications"] = [{"question": "指标？", "answer": "甲"}, {"question": "范围？", "answer": "全部"}]
    graph = agent.build_graph(MemorySaver())
    first = graph.invoke(state, config)
    pending = first["__interrupt__"][0].value
    assert pending["items"] == [
        {"dimension": "地区", "original": f"别名{n}", "options": [f"成员{n}"]} for n in ("一", "二", "三")
    ]
    assert pending["options"] == ["成员一"] and pending["dimension"] == "地区"
    assert box.clarification["items"] == pending["items"]
    answer = [
        {"dimension": item["dimension"], "original": item["original"], "value": item["options"][0]}
        for item in pending["items"]
    ]
    final = graph.invoke(Command(resume={"answer": answer}), config)
    assert final["phase"] == "done" and not final.get("__interrupt__")
    assert queries[0].filters[0].values == ["成员一", "成员二", "成员三"]
    assert len(model.seen) == 1 and len(calls) == 1
    assert final["clarifications"][-1]["answer"] == answer
    assert final["clarifications"][-1]["deterministic"] is True


def test_legacy_string_answers_can_confirm_more_than_two_values(monkeypatch):
    box, model, config, state, calls, queries = multi_value_run(monkeypatch)
    graph = agent.build_graph(MemorySaver())
    current = graph.invoke(state, config)
    for index, member in enumerate(("成员一", "成员二", "成员三")):
        assert len(current["__interrupt__"][0].value["items"]) == 3 - index
        box.restore(box.snapshot())  # 保存后恢复也必须保留此前的明确确认。
        current = graph.invoke(Command(resume={"answer": member}), config)
    assert current["phase"] == "done" and not current.get("__interrupt__")
    assert queries[0].filters[0].values == ["成员一", "成员二", "成员三"]
    assert len(model.seen) == 1 and all(item["deterministic"] for item in current["clarifications"])
    assert all(value.startswith("别名") for filters, _ in calls for item in filters for value in item["values"])
    assert {entry["value"] for entry in box.filter_alignment if entry["method"] == "confirmed"} == {
        "成员一",
        "成员二",
        "成员三",
    }


def test_partial_structured_answer_reprompts_remaining_items(monkeypatch):
    _, model, config, state, _, queries = multi_value_run(monkeypatch)
    graph = agent.build_graph(MemorySaver())
    graph.invoke(state, config)
    current = graph.invoke(
        Command(resume={"answer": [{"dimension": "地区", "original": "别名二", "value": "成员二"}]}), config
    )
    assert [item["original"] for item in current["__interrupt__"][0].value["items"]] == ["别名一", "别名三"]
    assert current["scope"]["filters"][0]["values"] == ["别名一", "成员二", "别名三"]
    assert not queries and len(model.seen) == 1


def test_duplicate_filter_values_share_one_pending_selection(monkeypatch):
    _, model, config, state, _, queries = multi_value_run(monkeypatch)
    model.replies[0] = scope_reply(filters=[{"dimension": "地区", "values": ["别名一", "别名一"]}])
    graph = agent.build_graph(MemorySaver())
    first = graph.invoke(state, config)
    pending = first["__interrupt__"][0].value
    assert pending["items"] == [{"dimension": "地区", "original": "别名一", "options": ["成员一"]}]
    answer = _clarification_answer(pending, [{"dimension": "地区", "original": "别名一", "value": "成员一"}])
    final = graph.invoke(Command(resume={"answer": answer}), config)
    assert final["phase"] == "done" and queries[0].filters[0].values == ["成员一", "成员一"]


@pytest.mark.parametrize("with_conversation", [False, True])
@pytest.mark.parametrize("model_kind", ["lookup", "chart"])
def test_free_text_correction_changes_only_pending_filter_dimension(monkeypatch, with_conversation, model_kind):
    confirmed = Scope(
        question_type="lookup",
        metric_codes=["m_a"],
        filters=[{"dimension": "地区", "values": ["别名"]}, {"dimension": "类别", "values": ["已确认"]}],
        dimensions=["地区"],
        start_date="2026-10-01",
        end_date="2026-10-31",
        limit=12,
    )
    # 模型尝试改其他范围，只有追问维度的筛选可以生效。
    corrected = scope_reply(
        model_kind,
        filters=[{"dimension": "地区", "values": ["纠正别名"]}, {"dimension": "类别", "values": ["不应采用"]}],
        dimensions=[],
        start_date="2025-01-01",
        end_date="2025-02-01",
        limit=1,
    )
    responses = [scope_reply(), corrected] if with_conversation else [corrected]
    box, model, config, state, _ = configured(monkeypatch, responses)
    for _, version in box.catalog.versions.values():
        version.dimensions = [{"name": "地区"}, {"name": "类别"}]
    state["inherited"] = {"scope": confirmed.model_dump(mode="json"), "user_text": "继续分析"}
    if with_conversation:
        state["inherited"]["conversation"] = {"turns": []}
        monkeypatch.setattr(agent, "_followup_inherited", lambda state, box: state["inherited"])
    checked = []

    def align(session, user, version, filters, settings, **kwargs):
        checked.append([item.model_dump() for item in filters])
        unresolved = []
        aligned = []
        for item in filters:
            if item.dimension == "地区" and item.values == ["别名"]:
                unresolved.append(
                    {"dimension": "地区", "original": "别名", "candidates": ["候选"], "has_dimension_table": False}
                )
            aligned.append(item.model_copy(update={"values": ["正式成员"]}) if item.values == ["纠正别名"] else item)
        return AlignmentResult(aligned, unresolved=unresolved)

    monkeypatch.setattr(agent, "align_filters", align)
    monkeypatch.setattr(agent, "has_dimension_table", lambda *args: False)
    monkeypatch.setattr(agent.service, "version_sources", lambda *args: {})
    _, queries = fake_ask(monkeypatch, box)
    graph = agent.build_graph(MemorySaver())
    graph.invoke(state, config)
    final = graph.invoke(Command(resume={"answer": "我指的是纠正别名，请改这个地区"}), config)
    expected = confirmed.model_dump(mode="json")
    expected["filters"][0]["values"] = ["正式成员"]
    assert final["scope"] == expected
    assert queries[0].filters[0].values == ["正式成员"]
    assert queries[0].filters[1].values == ["已确认"]
    assert checked[-1][0]["values"] == ["纠正别名"]
    assert len(model.seen) == (2 if with_conversation else 1)


def test_budget_exhausted_metric_overrides_other_metric_unresolved(monkeypatch):
    box, _, _, _, _, _ = multi_value_run(monkeypatch)
    first = box.catalog.versions["m_a"][1]
    second = SimpleNamespace(**vars(first))
    box.catalog.versions["m_b"] = (box.catalog.versions["m_b"][0], second)
    scope = Scope(
        question_type="relation",
        metric_codes=["m_a", "m_b"],
        filters=[DimensionFilter(dimension="地区", values=["别名"])],
    )

    def align(session, user, version, filters, settings, *, remaining_seconds):
        if version is first:
            return AlignmentResult(
                filters,
                unresolved=[
                    {"dimension": "地区", "original": "别名", "candidates": ["候选"], "has_dimension_table": False}
                ],
            )
        return AlignmentResult(
            filters, unknown=[{"dimension": "地区", "original": "别名", "reason": "budget_exhausted"}]
        )

    monkeypatch.setattr(agent, "align_filters", align)
    aligned, unresolved = agent._align_scope_filters(box, scope)
    assert not unresolved and aligned.filters[0].values == ["别名"]
    assert box.filter_alignment_unknown == [{"dimension": "地区", "original": "别名", "reason": "budget_exhausted"}]
