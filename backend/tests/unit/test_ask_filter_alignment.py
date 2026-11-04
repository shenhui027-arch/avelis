"""问数入口共用筛选对齐：意图、澄清候选和运行依据。"""

import json
from datetime import date
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from avelis.ask import agent, service
from avelis.ask import alignment as alignment_service
from avelis.ask.alignment import AlignmentResult
from avelis.ask.intent import DimensionFilter, Intent, MetricCard


@pytest.mark.parametrize("source", ["dimension_table", "member"])
def test_resume_adopts_only_actual_offered_candidates_in_new_toolbox(monkeypatch, source):
    box = make_box(monkeypatch)
    monkeypatch.setattr(service, "visible_catalog", lambda *a, **kw: box.catalog)
    monkeypatch.setattr(service, "dependency_drift", lambda *a: [])
    samples = [] if source == "dimension_table" else ["1", "2"]
    sample_calls = []

    def sample(*a, **kw):
        sample_calls.append(True)
        return {"dimension_a": samples.copy()}

    monkeypatch.setattr(service, "_dimension_values", sample)
    table = SimpleNamespace(asset=SimpleNamespace(id="dimension_source", catalog_name="c", db_name="d", name="t"))
    monkeypatch.setattr(
        alignment_service, "_dimension_tables", lambda *a: [table] if source == "dimension_table" else []
    )
    monkeypatch.setattr(alignment_service, "_table_matches", lambda *a: (["1", "2"], []))
    monkeypatch.setattr(service, "filter_alignment_restricted", lambda *a: False)

    def call(name, args):
        return AIMessage("", tool_calls=[{"id": name, "name": name, "args": args}])

    args = {
        "metric_codes": ["metric_a"],
        "start_date": "2026-01-01",
        "end_date": "2026-01-31",
        "filters": [{"dimension": "dimension_a", "values": ["alias_a"]}],
    }
    replies = iter(
        [
            call("submit_intent", args),
            call("ask_user", {"question": "choose", "options": ["1", "2"]}),
            call("submit_intent", {**args, "filters": [{"dimension": "dimension_a", "values": ["1"]}]}),
        ]
    )
    model = SimpleNamespace(bind_tools=lambda _: SimpleNamespace(invoke=lambda _: next(replies)))
    monkeypatch.setattr(agent.modelcfg, "resolve", lambda *a, **kw: SimpleNamespace(model=model, adapter="offline"))
    monkeypatch.setattr(agent.modelcfg, "policy", lambda *a: {"send_results": False})
    executions = []

    def fulfil(session, user, intent, answer, catalog):
        executions.append(intent)
        answer.status, answer.result, answer.message = "answered", {"columns": [], "rows": []}, "empty"
        answer.evidence = {}

    monkeypatch.setattr(service, "fulfil", fulfil)
    answer = SimpleNamespace(evidence={}, executed_at=None, cache_hit=False, error_code=None)
    config = {"configurable": {"thread_id": "ask-confirmed", "session": None, "user": None, "answer": answer}}
    graph = agent.build_graph(MemorySaver())
    first = graph.invoke({"turns": ["question"], "messages": [HumanMessage("question")]}, config)
    pending = first["__interrupt__"][0].value
    assert pending["alignment_candidates"][0]["candidates"] == ["1", "2"]
    assert answer.evidence["alignment_candidates"][0]["original"] == "alias_a"
    samples.clear()  # 连成员值候选也不能因事实表变空而重新追问。
    answer.evidence = {}  # 与 run_turn 一样，为恢复请求使用新的答案证据。
    final = graph.invoke(Command(resume="1"), config)
    assert not final.get("__interrupt__") and answer.status == "answered" and answer.result["rows"] == []
    assert len(executions) == 1 and executions[0].filters[0].values == ["1"]
    assert len(sample_calls) == 1
    assert answer.evidence["filter_alignment"] == [
        {
            "dimension": "dimension_a",
            "original": "alias_a",
            "value": "1",
            "method": "confirmed",
            **({"asset_id": "dimension_source"} if source == "dimension_table" else {}),
        }
    ]


def test_ask_confirmation_excludes_candidates_only_seen_by_model(monkeypatch):
    box = make_box(monkeypatch)
    box.alignment_candidates = [
        {
            "dimension": "dimension_a",
            "original": "alias_a",
            "candidates": ["1", "2"],
            "candidate_sources": {"1": ["asset_a"], "2": ["asset_a"]},
            "has_dimension_table": True,
        }
    ]
    box.ask_user("choose 1", ["1"])
    monkeypatch.setattr(agent, "interrupt", lambda _: "2")
    config = {"configurable": {"session": None, "user": None, "answer": None}}
    monkeypatch.setattr(service, "filter_alignment_restricted", lambda *a: False)
    resumed = agent.clarify({"clarification": box.clarification}, config)
    record = resumed["alignment_clarifications"][0]
    assert alignment_service.confirmed_values(record["items"], record["answer"]) == []
    box.confirmed = alignment_service.confirmed_values(record["items"], record["answer"])
    monkeypatch.setattr(service, "dependency_drift", lambda *a: [])
    monkeypatch.setattr(service, "_dimension_values", lambda *a: {"dimension_a": []})
    monkeypatch.setattr(alignment_service, "_dimension_tables", lambda *a: [])
    assert submit(box, value="2")["accepted"] is False
    assert not box.filter_alignment


def test_ask_confirmation_drops_items_the_question_did_not_offer(monkeypatch):
    """模型问的是第二个未解决项时，第一项没有展示任何候选，不能留在记录里抢走文字回答。"""
    box = make_box(monkeypatch)
    box.alignment_candidates = [
        {
            "dimension": "region",
            "original": "r",
            "candidates": ["10", "11"],
            "candidate_sources": {"10": ["a"], "11": ["a"]},
            "has_dimension_table": True,
        },
        {
            "dimension": "channel",
            "original": "c",
            "candidates": ["20", "21"],
            "candidate_sources": {"20": ["b"], "21": ["b"]},
            "has_dimension_table": True,
        },
    ]
    box.ask_user("which channel", ["20", "21"])
    assert [item["dimension"] for item in box.clarification["alignment_candidates"]] == ["channel"]
    monkeypatch.setattr(agent, "interrupt", lambda _: "20")
    monkeypatch.setattr(service, "filter_alignment_restricted", lambda *a: False)
    config = {"configurable": {"session": None, "user": None, "answer": None}}
    record = agent.clarify({"clarification": box.clarification}, config)["alignment_clarifications"][0]
    confirmed = alignment_service.confirmed_values(record["items"], record["answer"])
    assert [(c["dimension"], c["value"]) for c in confirmed] == [("channel", "20")]


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
