"""问数入口共用筛选对齐：意图、澄清候选和运行依据。"""

import json
from datetime import date
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from avelis.ask import agent, service
from avelis.ask import alignment as alignment_service
from avelis.ask.alignment import AlignmentResult
from avelis.ask.intent import DimensionFilter, Intent, MetricCard


def make_box(monkeypatch, *, codes=("metric_a",), statuses=None):
    dimensions = [{"name": "dimension_a", "column": "column_a"}]
    statuses = statuses or ["published"] * len(codes)
    cards = [
        MetricCard(code=code, name=code, aliases=[], dimensions=dimensions, status=status)
        for code, status in zip(codes, statuses)
    ]
    versions = {
        code: (None, SimpleNamespace(id=code, kind="atomic", status=status, dimensions=dimensions))
        for code, status in zip(codes, statuses)
    }
    settings = SimpleNamespace(business_timezone="Asia/Taipei", ask_max_tool_rounds=2, reply_language="auto")
    monkeypatch.setattr(agent, "get_settings", lambda: settings)
    monkeypatch.setattr(agent, "has_dimension_table", lambda *_: False)
    return agent.Toolbox(None, None, service.Catalog(cards, versions))


def alignment(value="member_a", *, original="alias_a", method="normalized", unresolved=None, unknown=None):
    return AlignmentResult(
        filters=[DimensionFilter(dimension="dimension_a", values=[value])],
        explanations=[]
        if unresolved or unknown
        else [{"dimension": "dimension_a", "original": original, "value": value, "method": method}],
        unresolved=unresolved or [],
        unknown=unknown or [],
    )


def submit(box, *, value="alias_a", codes=None):
    return json.loads(
        box.submit_intent(
            codes or [card.code for card in box.catalog.cards],
            date(2026, 1, 1),
            date(2026, 1, 31),
            filters=[{"dimension": "dimension_a", "values": [value]}],
        )
    )


@pytest.mark.parametrize("method", ["exact", "normalized", "dimension_table"])
def test_submit_accepts_aligned_value_and_retains_explanation(monkeypatch, method):
    box = make_box(monkeypatch)
    result = alignment(method=method)
    if method == "dimension_table":
        result.explanations[0].update(table="catalog_a.schema_a.dimension_a", column="alias_a")
    calls = []

    def align(session, user, version, filters, settings):
        calls.append((version.id, filters[0].values))
        return result

    monkeypatch.setattr(agent, "align_filters", align)
    assert submit(box)["accepted"] is True
    assert box.intent.filters[0].values == ["member_a"]
    assert box.filter_alignment == result.explanations
    assert calls == [("metric_a", ["alias_a"])]


def test_submit_rejects_unresolved_with_candidates(monkeypatch):
    box = make_box(monkeypatch)
    pending = {
        "dimension": "dimension_a",
        "original": "alias_a",
        "candidates": ["member_a", "member_b"],
        "has_dimension_table": False,
    }
    monkeypatch.setattr(agent, "align_filters", lambda *_: alignment("alias_a", unresolved=[pending]))
    out = submit(box)
    assert out["accepted"] is False
    assert out["unresolved_filters"] == [pending]
    assert "member_a" in out["errors"][0] and "member_b" in out["errors"][0]
    assert "ask_user" in out["errors"][0]
    assert "可在维表中维护别名并建立关联路径" in out["errors"][0]
    assert box.intent is None


def test_submit_uses_shared_normalization_and_keeps_contains_as_candidates(monkeypatch):
    box = make_box(monkeypatch)
    monkeypatch.setattr(service, "dependency_drift", lambda *_: [])
    monkeypatch.setattr(service, "_dimension_values", lambda *_: {"dimension_a": ["MEMBER A", "member_b"]})
    monkeypatch.setattr(alignment_service, "_dimension_tables", lambda *_: [])
    assert submit(box, value=" ｍｅｍｂｅｒ ａ ")["accepted"] is True
    assert box.intent.filters[0].values == ["MEMBER A"]
    assert box.filter_alignment[0]["method"] == "normalized"
    out = submit(box, value="member_b_suffix")
    assert out["accepted"] is False
    assert out["unresolved_filters"][0]["candidates"] == ["member_b"]
    assert box.intent is None
    assert box.filter_alignment == []


def test_multiple_metrics_must_agree_before_rewriting(monkeypatch):
    box = make_box(monkeypatch, codes=("metric_a", "metric_b"))
    checked = []

    def align(session, user, version, filters, settings):
        checked.append(version.id)
        return alignment()

    monkeypatch.setattr(agent, "align_filters", align)
    assert submit(box)["accepted"] is True
    assert box.intent.filters[0].values == ["member_a"]
    assert checked == ["metric_a", "metric_b"]
    assert len(box.filter_alignment) == 1


def test_multiple_metric_conflict_is_unresolved(monkeypatch):
    box = make_box(monkeypatch, codes=("metric_a", "metric_b"))

    def align(session, user, version, filters, settings):
        return alignment("member_a" if version.id == "metric_a" else "member_b")

    monkeypatch.setattr(agent, "align_filters", align)
    out = submit(box)
    assert out["accepted"] is False
    assert out["unresolved_filters"][0]["candidates"] == ["member_a", "member_b"]


def test_normalized_conflict_reports_available_dimension_table(monkeypatch):
    box = make_box(monkeypatch, codes=("metric_a", "metric_b"))
    checked = []

    def align(session, user, version, filters, settings):
        return alignment("member_a" if version.id == "metric_a" else "member_b")

    def has_table(session, user, version, dimension):
        checked.append((version.id, dimension))
        return version.id == "metric_b"

    monkeypatch.setattr(agent, "align_filters", align)
    monkeypatch.setattr(agent, "has_dimension_table", has_table)
    out = submit(box)
    assert out["accepted"] is False
    assert out["unresolved_filters"][0]["has_dimension_table"] is True
    assert checked == [("metric_a", "dimension_a"), ("metric_b", "dimension_a")]
    assert "可在维表中维护别名并建立关联路径" not in out["errors"][0]


def test_second_metric_unresolved_prevents_first_metric_alignment(monkeypatch):
    box = make_box(monkeypatch, codes=("metric_a", "metric_b"))
    pending = {
        "dimension": "dimension_a",
        "original": "alias_a",
        "candidates": ["member_b"],
        "has_dimension_table": True,
    }

    def align(session, user, version, filters, settings):
        return alignment() if version.id == "metric_a" else alignment("alias_a", unresolved=[pending])

    monkeypatch.setattr(agent, "align_filters", align)
    out = submit(box)
    assert out["accepted"] is False
    assert set(out["unresolved_filters"][0]["candidates"]) == {"member_a", "member_b"}
    assert out["unresolved_filters"][0]["has_dimension_table"] is True


def test_unknown_preserves_original_and_records_reason(monkeypatch):
    box = make_box(monkeypatch, codes=("metric_a", "metric_b"))
    unknown = {"dimension": "dimension_a", "original": "alias_a", "reason": "query_failed"}

    def align(session, user, version, filters, settings):
        return alignment() if version.id == "metric_a" else alignment("alias_a", unknown=[unknown])

    monkeypatch.setattr(agent, "align_filters", align)
    assert submit(box)["accepted"] is True
    assert box.intent.filters[0].values == ["alias_a"]
    assert box.filter_alignment == []
    assert box.filter_alignment_unknown == [unknown]


@pytest.mark.parametrize("statuses", [("affected",), ("published", "retired")])
def test_unavailable_metrics_keep_existing_governance_priority(monkeypatch, statuses):
    box = make_box(monkeypatch, codes=tuple(f"metric_{i}" for i in range(len(statuses))), statuses=statuses)
    monkeypatch.setattr(agent, "align_filters", lambda *_: pytest.fail("治理拦截前不应查筛选取值"))
    assert submit(box)["accepted"] is True
    assert box.intent.filters[0].values == ["alias_a"]


def test_invalid_dimension_is_rejected_before_alignment(monkeypatch):
    box = make_box(monkeypatch)
    monkeypatch.setattr(agent, "align_filters", lambda *_: pytest.fail("无效维度不应查筛选取值"))
    out = json.loads(
        box.submit_intent(
            ["metric_a"],
            date(2026, 1, 1),
            date(2026, 1, 31),
            filters=[{"dimension": "dimension_b", "values": ["alias_a"]}],
        )
    )
    assert out["accepted"] is False
    assert "dimension_b" in out["errors"][0]


@pytest.mark.parametrize("unknown", [False, True])
def test_understand_writes_alignment_to_steps_and_answer_evidence(monkeypatch, unknown):
    box = make_box(monkeypatch)
    result = (
        alignment("alias_a", unknown=[{"dimension": "dimension_a", "original": "alias_a", "reason": "query_failed"}])
        if unknown
        else alignment()
    )
    monkeypatch.setattr(agent, "align_filters", lambda *_: result)
    monkeypatch.setattr(service, "visible_catalog", lambda *_args, **_kwargs: box.catalog)
    reply = AIMessage(
        "",
        tool_calls=[
            {
                "id": "submit_1",
                "name": "submit_intent",
                "args": {
                    "metric_codes": ["metric_a"],
                    "start_date": "2026-01-01",
                    "end_date": "2026-01-31",
                    "filters": [{"dimension": "dimension_a", "values": ["alias_a"]}],
                },
            }
        ],
    )
    model = SimpleNamespace(bind_tools=lambda _: SimpleNamespace(invoke=lambda _: reply))
    monkeypatch.setattr(agent.modelcfg, "resolve", lambda *_: SimpleNamespace(model=model, adapter="fake-model"))
    monkeypatch.setattr(agent.modelcfg, "policy", lambda *_: {"send_results": False})
    answer = SimpleNamespace(evidence={})
    out = agent.understand({"messages": []}, {"configurable": {"session": None, "user": None, "answer": answer}})
    assert out["intent"]["filters"][0]["values"] == ["alias_a" if unknown else "member_a"]
    assert out["steps"][0]["filter_alignment"] == result.explanations
    assert answer.evidence["filter_alignment"] == result.explanations
    assert answer.evidence["filter_alignment_unknown"] == result.unknown


def test_govern_preserves_alignment_when_fulfil_replaces_evidence(monkeypatch):
    result = alignment()
    unknown = [{"dimension": "dimension_a", "original": "alias_a", "reason": "query_failed"}]
    answer = SimpleNamespace(
        evidence={"filter_alignment": result.explanations, "filter_alignment_unknown": unknown},
        status="blocked",
        error_code="metric_affected",
        executed_at=None,
        cache_hit=False,
    )
    monkeypatch.setattr(service, "visible_catalog", lambda *_args, **_kwargs: object())

    def fulfil(session, user, intent, answer, catalog):
        answer.evidence = {"plan": {"status": "blocked"}}

    monkeypatch.setattr(service, "fulfil", fulfil)
    agent.govern(
        {"intent": Intent(metric_code="metric_a").model_dump(mode="json")},
        {"configurable": {"session": None, "user": None, "answer": answer}},
    )
    assert answer.evidence["filter_alignment"] == result.explanations
    assert answer.evidence["filter_alignment_unknown"] == unknown
    assert answer.evidence["plan"]["status"] == "blocked"


def test_demo_adapter_and_dimension_matching_do_not_call_alignment(monkeypatch):
    box = make_box(monkeypatch)
    monkeypatch.setattr(agent, "align_filters", lambda *_: pytest.fail("演示模型及匹配工具行为保持不变"))
    monkeypatch.setattr(service, "_dimension_values", lambda *_: {"dimension_a": ["member_a", "member_b"]})
    assert json.loads(box.match_dimension_values("metric_a", "dimension_a", "member_a"))["matched"] == ["member_a"]
    monkeypatch.setattr(service, "visible_catalog", lambda *_args, **_kwargs: box.catalog)
    monkeypatch.setattr(agent.modelcfg, "resolve", lambda *_: None)
    monkeypatch.setattr(agent.DemoIntentModel, "parse", lambda *_args, **_kwargs: Intent(metric_code="metric_a"))
    out = agent.understand(
        {"turns": ["question"]},
        {"configurable": {"session": None, "user": None, "answer": SimpleNamespace(evidence={})}},
    )
    assert out["intent"]["metric_code"] == "metric_a"
    assert out["steps"][0]["adapter"] == "demo-rules"
