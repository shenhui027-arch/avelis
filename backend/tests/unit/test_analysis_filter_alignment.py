"""筛选值对齐接入真实分析图；模型与数据源查询使用替身。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from avelis.analysis import agent, service
from avelis.analysis.tools import Scope
from avelis.api.schemas import AnalysisRunOut, FilterAlignmentOut
from avelis.ask import alignment
from avelis.ask.alignment import AlignmentResult
from avelis.ask.intent import DimensionFilter
from avelis.models import Asset
from tests.unit.test_analysis_entry import configured, fake_ask, scope_reply
from tests.unit.test_analysis_hitl import reply, waiting_run
from tests.unit.test_analysis_pipeline import Phased


def configured_alignment(monkeypatch, *, table=True, unattended=False, question_type="lookup"):
    box, model, config, state, prechecks = configured(
        monkeypatch,
        [scope_reply(question_type, filters=[{"dimension": "地区", "values": ["华东区"]}])],
        unattended=unattended,
    )
    for _, version in box.catalog.versions.values():
        version.status = "published"
    monkeypatch.setattr(alignment.service, "dependency_drift", lambda session, version: False)
    monkeypatch.setattr(alignment.service, "version_sources", lambda session, version: {})
    monkeypatch.setattr(
        alignment.service,
        "_dimension_values",
        lambda session, user, version, settings, **kwargs: {"地区": ["华东", "华南"]},
    )
    dimension_table = SimpleNamespace(
        asset=SimpleNamespace(id="region-alias-asset", catalog_name="c", db_name="d", name="region_alias")
    )
    monkeypatch.setattr(alignment, "_dimension_tables", lambda *args: [dimension_table] if table else [])
    monkeypatch.setattr(alignment, "_table_matches", lambda *args, **kwargs: (["华东"], ["display_name"]))
    state["question"] = "华东区各城市的门店成交额"
    return box, model, config, state, prechecks


def test_lookup_aligns_with_dimension_table_and_exposes_explanation(monkeypatch):
    box, model, config, state, prechecks = configured_alignment(monkeypatch)
    answer, queries = fake_ask(monkeypatch, box)

    final = agent.build_graph(MemorySaver()).invoke(state, config)

    assert final["phase"] == "done" and not final.get("__interrupt__")
    assert queries[0].filters[0].values == answer.intent["filters"][0]["values"] == ["华东"]
    expected = {
        "dimension": "地区",
        "original": "华东区",
        "value": "华东",
        "method": "dimension_table",
        "asset_id": "region-alias-asset",
        "table": "c.d.region_alias",
        "column": "display_name",
    }
    assert answer.evidence["filter_alignment"] == box.report["evidence"]["filter_alignment"] == [expected]
    step = next(step for step in box.session.added if getattr(step, "tool", None) == "understand")
    assert step.output["scope"]["filters"][0]["values"] == ["华东"]
    assert step.output["filter_alignment"] == [expected]
    assert box.snapshot()["state"]["filter_alignment"] == [expected]
    assert len(model.seen) == 1 and prechecks == []


def test_without_dimension_table_requests_structured_clarification(monkeypatch):
    box, model, config, state, prechecks = configured_alignment(monkeypatch, table=False)
    _, queries = fake_ask(monkeypatch, box)

    final = agent.build_graph(MemorySaver()).invoke(state, config)

    pending = final["__interrupt__"][0].value
    assert pending["kind"] == "clarify" and pending["dimension"] == "地区"
    assert pending["options"] == ["华东"] and "华东" in pending["question"]
    assert "可在维表中维护别名并建立关联路径" in pending["question"]
    assert box.clarification["options"] == ["华东"] and box.clarification["dimension"] == "地区"
    assert final["scope"]["filters"][0]["values"] == ["华东区"]
    assert len(model.seen) == 1 and prechecks == queries == []


def test_clicking_candidate_continues_without_model_reinterpreting_scope(monkeypatch):
    box, model, config, state, _ = configured_alignment(monkeypatch, table=False)
    answer, queries = fake_ask(monkeypatch, box)
    # 若恢复错误地再次问模型，它仍会重复原别名；选中的候选应直接生效。
    model.replies.append(scope_reply(filters=[{"dimension": "地区", "values": ["华东区"]}]))
    graph = agent.build_graph(MemorySaver())
    first = graph.invoke(state, config)

    final = graph.invoke(Command(resume={"answer": "华东"}), config)

    assert final["phase"] == "done" and not final.get("__interrupt__")
    assert len(model.seen) == 1 and queries[0].filters[0].values == ["华东"]
    assert answer.intent["filters"][0]["values"] == ["华东"] and box.clarification is None
    assert final["clarifications"] == [
        {
            "question": first["clarification"],
            "answer": "华东",
            "options": ["华东"],
            "dimension": "地区",
            "items": [{"dimension": "地区", "original": "华东区", "options": ["华东"]}],
            "deterministic": True,
        }
    ]


@pytest.mark.parametrize("source", ["dimension_table", "member"])
@pytest.mark.parametrize("structured", [False, True])
def test_confirmed_candidate_executes_empty_lookup_without_realigning(monkeypatch, source, structured):
    box, model, config, state, prechecks = configured_alignment(monkeypatch, table=source == "dimension_table")
    asset = Asset(id="region-alias-asset", domain_id="dA", status="active")
    box.session.store[(Asset, asset.id)] = asset
    monkeypatch.setattr(alignment.service, "upstream_closure", lambda s, ids: {asset.id: asset})
    sample = [] if source == "dimension_table" else ["华东", "华南"]
    monkeypatch.setattr(alignment.service, "_dimension_values", lambda *args, **kwargs: {"地区": sample})
    table_queries = []

    def table_matches(session, version, table, original, *args):
        table_queries.append(original)
        return (["1", "2"], ["display_name"]) if original == "华东区" else ([], [])

    monkeypatch.setattr(alignment, "_table_matches", table_matches)
    aligned_inputs = []
    align = agent.align_filters

    def counted_align(session, user, version, filters, *args, **kwargs):
        aligned_inputs.append([item.model_dump() for item in filters])
        return align(session, user, version, filters, *args, **kwargs)

    monkeypatch.setattr(agent, "align_filters", counted_align)
    answer, queries = fake_ask(monkeypatch, box, explanation="没有符合条件的数据")
    answer.result = {"columns": ["指标甲"], "rows": [], "row_count": 0, "truncated": False}
    graph = agent.build_graph(MemorySaver())
    first = graph.invoke(state, config)
    pending = first["__interrupt__"][0].value
    value = "1" if source == "dimension_table" else "华东"
    if source == "dimension_table":
        assert pending["items"][0]["candidate_sources"] == {"1": [asset.id], "2": [asset.id]}
    # 即使事实表成员已不在样本中，明确选择的成员值仍应直接采用。
    sample.clear()
    feedback = [{"dimension": "地区", "original": "华东区", "value": value}] if structured else value
    final = graph.invoke(Command(resume={"answer": feedback}), config)

    assert final["phase"] == "done" and not final.get("__interrupt__")
    assert len(queries) == 1 and queries[0].filters[0].values == [value]
    assert answer.status == "answered" and answer.result["rows"] == []
    assert prechecks == [] and len(model.seen) == 1 and len(aligned_inputs) == 2
    assert aligned_inputs[-1][0]["values"] == [value]  # 确认也交给共用函数消费。
    assert table_queries == (["华东区"] if source == "dimension_table" else [])
    expected = {"dimension": "地区", "original": "华东区", "value": value, "method": "confirmed"}
    if source == "dimension_table":
        expected["asset_id"] = asset.id
    assert answer.evidence["filter_alignment"] == box.report["evidence"]["filter_alignment"] == [expected]
    assert box.snapshot()["state"]["filter_alignment"] == [expected]
    assert FilterAlignmentOut.model_validate(expected).method == "confirmed"


def test_unattended_unresolved_filter_ends_without_interrupt_or_query(monkeypatch):
    box, model, config, state, prechecks = configured_alignment(monkeypatch, table=False, unattended=True)
    _, queries = fake_ask(monkeypatch, box)

    final = agent.build_graph(MemorySaver()).invoke(state, config)

    assert final["phase"] == "clarify" and not final.get("__interrupt__")
    assert final["clarification"] is None and "华东" in final["notes"][0]
    assert box.clarification["options"] == ["华东"] and box.clarification["dimension"] == "地区"
    assert len(model.seen) == 1 and prechecks == queries == []


def test_precheck_uses_aligned_filters_and_report_keeps_evidence(monkeypatch):
    box, _, config, state, _ = configured_alignment(monkeypatch, question_type="change")
    understood = agent.understand(state, config)
    captured = []
    monkeypatch.setattr(box, "precheck", lambda scope: captured.append(scope) or ["D1"])

    agent.precheck({**state, **understood}, config)
    box.submit_report(title="标题", summary="摘要", claims=[], conclusion="insufficient_evidence")

    assert captured[0].filters[0].values == ["华东"]
    assert box.report["evidence"]["filter_alignment"][0]["original"] == "华东区"


@pytest.mark.parametrize("source", ["report", "snapshot"])
def test_run_view_exposes_typed_filter_alignment_from_saved_evidence(monkeypatch, source):
    session, _, run, _ = waiting_run(monkeypatch)
    run.created_at = service.utcnow()
    explanation = {"dimension": "地区", "original": "华东区", "value": "华东", "method": "normalized"}
    if source == "report":
        run.report = {"evidence": {"filter_alignment": [explanation]}}
    else:
        run.checkpoint["snapshot"]["state"] = {"filter_alignment": [explanation]}

    view = service.run_view(session, run)

    assert view["filter_alignment"] == [explanation]
    assert AnalysisRunOut.model_validate(view).filter_alignment[0].value == "华东"
    assert service.run_view(session, run, full=False)["filter_alignment"] == []


@pytest.mark.parametrize("with_conversation", [False, True])
def test_confirmed_non_chart_followup_rechecks_retained_scope(monkeypatch, with_conversation):
    box, _, config, state, _ = configured_alignment(monkeypatch)
    confirmed = Scope(
        question_type="lookup", metric_codes=["m_a"], filters=[DimensionFilter(dimension="地区", values=["华东区"])]
    )
    state["inherited"] = {"scope": confirmed.model_dump(mode="json"), "user_text": "继续解释"}
    if with_conversation:
        state["inherited"]["conversation"] = {"turns": []}
        monkeypatch.setattr(agent, "_followup_inherited", lambda state, box: state["inherited"])
        config["configurable"]["model"] = Phased(
            [reply("submit_scope", {"question_type": "relation", "metric_codes": ["m_b"], "filters": []}, "followup")]
        )

    result = agent.understand(state, config)

    assert result["scope"]["metric_codes"] == ["m_a"]
    assert result["scope"]["filters"][0]["values"] == ["华东"]
    assert box.filter_alignment[0]["method"] == "dimension_table"


@pytest.mark.parametrize("kind", ["conflicting", "conflicting_with_path", "unknown", "not_shared"])
def test_multi_metric_alignment_requires_shared_dimension_and_agreement(monkeypatch, kind):
    box, _, _, _, _ = configured_alignment(monkeypatch)
    first = box.catalog.versions["m_a"][1]
    second = SimpleNamespace(**vars(first))
    box.catalog.versions["m_b"] = (box.catalog.versions["m_b"][0], second)
    scope = Scope(
        question_type="relation", metric_codes=["m_a", "m_b"], filters=[{"dimension": "地区", "values": ["华东区"]}]
    )
    calls = []

    def align(session, user, version, filters, settings, **kwargs):
        calls.append(version)
        if kind == "unknown" and version is second:
            return AlignmentResult(filters, unknown=[{"dimension": "地区", "original": "华东区", "reason": "timeout"}])
        value = "华南" if version is second else "华东"
        return AlignmentResult(
            [DimensionFilter(dimension="地区", values=[value])],
            explanations=[{"dimension": "地区", "original": "华东区", "value": value, "method": "normalized"}],
        )

    if kind == "not_shared":
        second.dimensions = []
        monkeypatch.setattr(agent, "align_filters", lambda *args, **kwargs: AlignmentResult(args[3]))
    else:
        monkeypatch.setattr(agent, "align_filters", align)
    monkeypatch.setattr(agent, "has_dimension_table", lambda *args: kind == "conflicting_with_path")

    aligned, unresolved = agent._align_scope_filters(box, scope)

    assert aligned.filters[0].values == ["华东区"] and box.filter_alignment == []
    if kind in ("conflicting", "conflicting_with_path"):
        assert unresolved[0]["candidates"] == ["华东", "华南"] and len(calls) == 2
        assert unresolved[0]["has_dimension_table"] == (kind == "conflicting_with_path")
    elif kind == "not_shared":
        assert unresolved[0]["dimension"] == "地区" and unresolved[0]["candidates"] == []
    else:
        assert unresolved == [] and box.filter_alignment_unknown[0]["reason"] == "timeout"


def test_multi_metric_disagreement_keeps_the_exact_original_as_a_candidate(monkeypatch):
    """一个口径里原值精确存在、另一个口径规范化成别的成员：原值也是合法选项，不能只给另一个。"""
    box, _, _, _, _ = configured_alignment(monkeypatch)
    first = box.catalog.versions["m_a"][1]
    second = SimpleNamespace(**vars(first))
    box.catalog.versions["m_b"] = (box.catalog.versions["m_b"][0], second)
    scope = Scope(
        question_type="relation", metric_codes=["m_a", "m_b"], filters=[{"dimension": "地区", "values": ["West"]}]
    )

    def align(session, user, version, filters, settings, **kwargs):
        if version is second:
            return AlignmentResult(
                [DimensionFilter(dimension="地区", values=["WEST"])],
                explanations=[{"dimension": "地区", "original": "West", "value": "WEST", "method": "normalized"}],
            )
        return AlignmentResult([DimensionFilter(dimension="地区", values=["West"])])

    monkeypatch.setattr(agent, "align_filters", align)
    monkeypatch.setattr(agent, "has_dimension_table", lambda *args: False)
    _aligned, unresolved = agent._align_scope_filters(box, scope)
    assert unresolved[0]["candidates"] == ["West", "WEST"]
