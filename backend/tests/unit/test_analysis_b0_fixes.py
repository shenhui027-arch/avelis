"""共同基线 B0 的底座修复（docs/agent-v2/diagnosis.md 第 4 节）：每项断言实际的状态与数据，不以返回 200 或出现关键字为据。

A. 理解阶段写了追问（Scope.clarification）或选了候选以外的指标：运行停在待澄清，保存可恢复的上下文，不继续预检与推理；
   恢复时预算接着计数。
E. 模型接入按提供方官方协议：reasoning_content 原样往返（HTTP 层用伪造的提供方核对）、截断与编号问题按基础设施错误、
   重试计入模型请求预算。
F. 查询复用不改变请求语义：排序、行数、列顺序、对比期、按日 / 按月、筛选、权限与口径版本变化时都不复用已有数据集。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import BaseModel

from avelis.analysis import agent, service
from avelis.analysis.tools import Budget
from avelis.llm_protocol import ModelProtocolError, ModelTruncated, ReasoningRoundTripChatOpenAI
from tests.unit.test_analysis_pipeline import WINDOW, Phased, catalog
from tests.unit.test_analysis_toolbox import make_box

CONTEXT = {"today": "2026-10-30", "tz": "UTC", "window": WINDOW}


def scope_reply(**scope):
    return AIMessage(content="", tool_calls=[{"name": "submit_scope", "args": {"question_type": "change", **scope},
                                              "id": "s1"}])


def run_graph(box, model, question="地区甲的甲量为什么下降？"):
    graph = agent.build_graph(MemorySaver())
    config = {"configurable": {"thread_id": "t1", "toolbox": box, "model": model}}
    graph.invoke(agent.initial_state(question, "2026-10-30", "UTC", WINDOW), config)
    return graph.get_state(config).values


# --- A. 澄清 ---------------------------------------------------------------------------------------

def test_clarification_stops_the_run_before_precheck_and_reasoning(monkeypatch):
    box = make_box(budget=Budget(), catalog=catalog(), monkeypatch=monkeypatch)
    box.run.window, box.run.question = dict(WINDOW), "地区甲的甲量为什么下降？"
    monkeypatch.setattr(box, "precheck", lambda scope: pytest.fail("待澄清时不应执行预检"))
    model = Phased([scope_reply(metric_codes=["m_a"], filters=[{"dimension": "地区", "values": ["甲"]}],
                                clarification="“甲量”是指件数还是金额？")])
    state = run_graph(box, model)
    assert state["phase"] == "clarify" and len(model.seen) == 1  # 只有理解一次模型请求，没有进入推理
    assert box.report is None and box.verification is None
    c = box.clarification
    assert c["question"] == "“甲量”是指件数还是金额？"
    # 可恢复的上下文：模型给出的范围（含原始参数）、原问题与窗口、此刻已用的预算
    assert c["scope"]["metric_codes"] == ["m_a"] and c["raw_scope"]["clarification"] == c["question"]
    assert c["resume"] == {"run_question": "地区甲的甲量为什么下降？", "window": WINDOW}
    assert c["budget"]["used"]["queries"] == 0 and c["budget"]["used"]["attempts"] == 0
    waiting = [s for s in box.session.added if getattr(s, "kind", None) == "clarify"]
    assert len(waiting) == 1 and waiting[0].status == "waiting" and waiting[0].output["question"] == c["question"]


def test_metric_codes_outside_the_candidates_are_not_dropped_silently(monkeypatch):
    box = make_box(budget=Budget(), catalog=catalog(), monkeypatch=monkeypatch)
    monkeypatch.setattr(box, "precheck", lambda scope: pytest.fail("待澄清时不应执行预检"))
    state = run_graph(box, Phased([scope_reply(metric_codes=["m_a", "m_zz"])]))
    assert state["phase"] == "clarify"
    assert box.plan["scope"]["metric_codes"] == ["m_a", "m_zz"]  # 原样保存，不删
    assert "m_zz" in box.clarification["question"] and "不在候选口径中：m_zz" in box.plan["problem"]


def test_scope_without_clarification_still_runs_the_precheck(monkeypatch):
    box = make_box(budget=Budget(), catalog=catalog(), monkeypatch=monkeypatch)
    seen = []
    monkeypatch.setattr(box, "precheck", lambda scope: seen.append(scope.metric_codes) or [])
    model = Phased([scope_reply(metric_codes=["m_a"], clarification="  "),
                    AIMessage(content="", tool_calls=[])])
    monkeypatch.setattr(agent, "act", lambda state, config: {"messages": [], "phase": "verify", "rounds": 1, "notes": []})
    monkeypatch.setattr(agent, "verify", lambda state, config: {"phase": "done"})
    state = run_graph(box, model)
    assert seen == [["m_a"]] and box.clarification is None and state["phase"] == "done"


def test_budget_restore_keeps_counting_after_clarification():
    budget = Budget(tool_calls=30, queries=20, seconds=300, explore=8)
    budget.used_tools, budget.reports_total, budget.used_calls = 5, 1, 7
    budget.used_queries, budget.used_explore = 4, 2
    view = budget.view()
    view["used"]["seconds"] = 40.0
    restored = Budget.restore(view)
    assert restored.view()["used"] | {"seconds": 40.0} == view["used"]  # 已用次数原样恢复，不清零
    assert (restored.used_queries, restored.used_explore, restored.reports_total) == (4, 2, 1)
    assert 259 <= restored.remaining() <= 260  # 已用的 40 秒从截止时间里扣除，等待用户的时间不计入
    assert restored.view()["limits"] == view["limits"]


def test_clarify_runs_end_the_job_normally_and_pause_scheduled_tasks():
    run = SimpleNamespace(status="clarify", error=None, report={"summary": "请确认指标"})
    assert service._outcome(run) == {"status": "succeeded", "reason": None, "run_status": "clarify"}
    assert service._outcome(SimpleNamespace(status="failed", error="x"))["status"] == "failed"


# --- E. 模型接入协议 ------------------------------------------------------------------------------

THOUGHT = "私有思考：先查总量再拆维度"


class _Args(BaseModel):
    value: str


def fake_deepseek(bodies: list[dict]) -> httpx.Client:
    """按官方文档的规则伪造 DeepSeek：带 tools 的多轮请求里，之前的助手消息没有回传 reasoning_content 就返回 400。"""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        assistants = [m for m in body["messages"] if m["role"] == "assistant"]
        if body.get("tools") and any("reasoning_content" not in m for m in assistants):
            return httpx.Response(400, json={"error": {"message": "reasoning_content must be passed back",
                                                       "type": "invalid_request_error"}})
        if not assistants:  # 第一轮：思考 + 两个并行工具调用，没有 content
            calls = [{"id": f"call_{v}", "type": "function",
                      "function": {"name": "echo", "arguments": json.dumps({"value": v})}} for v in ("a", "b")]
            message = {"role": "assistant", "content": None, "reasoning_content": THOUGHT, "tool_calls": calls}
            finish = "tool_calls"
        else:
            message, finish = {"role": "assistant", "content": "完成", "reasoning_content": "第二轮思考"}, "stop"
        return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 1, "model": "deepseek-flash",
                                         "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
    return httpx.Client(transport=httpx.MockTransport(handler))


def two_turns(cls) -> tuple[list, list[dict]]:
    bodies: list[dict] = []
    model = cls(model="deepseek-flash", api_key="unit-test-key", base_url="https://deepseek.invalid",
                http_client=fake_deepseek(bodies), max_retries=0).bind_tools(
        [StructuredTool.from_function(lambda value: value, name="echo", args_schema=_Args, description="echo")])
    messages = [HumanMessage("调用 echo")]
    first = model.invoke(messages)
    messages += [first, *(ToolMessage(content="ok", tool_call_id=c["id"]) for c in first.tool_calls)]
    return [*messages, model.invoke(messages)], bodies


def test_reasoning_content_round_trips_on_the_wire_per_the_official_protocol():
    messages, bodies = two_turns(ReasoningRoundTripChatOpenAI)
    first, last = messages[1], messages[-1]
    assert first.additional_kwargs["reasoning_content"] == THOUGHT and first.content == ""  # 不进入正文
    assert [c["id"] for c in first.tool_calls] == ["call_a", "call_b"]
    sent = [m for m in bodies[1]["messages"] if m["role"] == "assistant"][0]
    assert sent["reasoning_content"] == THOUGHT and [c["id"] for c in sent["tool_calls"]] == ["call_a", "call_b"]
    assert [m["tool_call_id"] for m in bodies[1]["messages"] if m["role"] == "tool"] == ["call_a", "call_b"]
    assert last.content == "完成"
    # 通用的 ChatOpenAI（B_raw 的实现）丢掉了这个字段：按官方协议的服务端会拒绝第二轮
    with pytest.raises(openai.BadRequestError):
        two_turns(ChatOpenAI)


def test_reasoning_survives_checkpoint_serialization():
    serde = JsonPlusSerializer()
    reply = AIMessage(content="", additional_kwargs={"reasoning_content": THOUGHT},
                      tool_calls=[{"name": "echo", "args": {"value": "a"}, "id": "call_a"}])
    restored = serde.loads_typed(serde.dumps_typed({"messages": [reply]}))["messages"][0]
    assert restored.additional_kwargs["reasoning_content"] == THOUGHT and restored.tool_calls[0]["id"] == "call_a"


class Replies:
    """按顺序返回回复（或抛出异常）的模型替身。"""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), 0

    def bind_tools(self, tools, **kwargs):
        return self

    def invoke(self, messages):
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def model_steps(box):
    return [s for s in box.session.added if getattr(s, "kind", None) == "model"]


def search(call_id, keyword="x"):
    return {"name": "search_metrics", "args": {"keyword": keyword}, "id": call_id}


def test_reasoning_is_not_written_to_step_records(monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    reply = AIMessage(content="", additional_kwargs={"reasoning_content": THOUGHT}, tool_calls=[search("c1")])
    out = agent._round({"messages": []}, box, Replies(reply))
    assert out[0].additional_kwargs["reasoning_content"] == THOUGHT  # 留在消息里，供下一轮回传
    recorded = json.dumps([[s.args, s.output] for s in box.session.added if hasattr(s, "output")], ensure_ascii=False)
    assert THOUGHT not in recorded


def test_truncated_reply_is_an_infrastructure_error_not_model_content(monkeypatch):
    box = make_box(monkeypatch=monkeypatch)

    def cut():
        return AIMessage(content="", response_metadata={"finish_reason": "length"}, tool_calls=[search("c1")])
    with pytest.raises(ModelTruncated):
        agent._round({"messages": []}, box, Replies(cut()))
    assert box.budget.used_calls == 0  # 截断的工具调用不执行
    step = model_steps(box)[-1]
    assert step.status == "error" and step.output["error_code"] == "model_truncated"
    # 理解阶段同样不把截断当成“没有结果”继续
    with pytest.raises(ModelTruncated):
        agent.understand({"question": "q", "context": {}}, {"configurable": {"toolbox": box, "model": Replies(cut())}})


def test_parallel_tool_call_ids_are_answered_one_by_one_and_must_be_unique(monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    out = agent._round({"messages": []}, box, Replies(AIMessage(content="", tool_calls=[search("a"), search("b", "y")])))
    assert [m.tool_call_id for m in out if isinstance(m, ToolMessage)] == ["a", "b"] and box.budget.used_calls == 2
    duplicate = AIMessage(content="", tool_calls=[search("a"), search("a", "y")])
    missing = AIMessage(content="", invalid_tool_calls=[{"name": "search_metrics", "args": "{bad", "id": None,
                                                         "error": "x"}])
    for bad in (duplicate, missing):
        with pytest.raises(ModelProtocolError):
            agent._round({"messages": []}, box, Replies(bad))
    # 参数无效但有编号的调用：照常计数并按编号回复，模型可以改正后重交
    invalid = AIMessage(content="", invalid_tool_calls=[{"name": "query_metric", "args": "{bad", "id": "c9", "error": "x"}])
    used = box.budget.used_calls
    out = agent._round({"messages": []}, box, Replies(invalid))
    assert box.budget.used_calls == used + 1 and out[-1].tool_call_id == "c9" and "参数" in out[-1].content


def test_empty_reply_without_content_is_legal_and_nudged(monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    out = agent._round({"messages": []}, box, Replies(AIMessage(content="", response_metadata={"finish_reason": "stop"})))
    assert isinstance(out[-1], HumanMessage) and box.budget.used_model_requests == 1


def test_retries_count_against_the_model_request_budget(monkeypatch):
    class APIConnectionError(Exception):  # 与 openai 的传输错误同名：按临时错误重试一次
        pass

    box = make_box(monkeypatch=monkeypatch)
    model = Replies(APIConnectionError("reset"), AIMessage(content="", tool_calls=[]))
    agent._round({"messages": []}, box, model)
    assert model.calls == 2 and box.budget.used_model_requests == 2
    first, second = model_steps(box)
    assert first.status == "error" and first.output["retry"] is True and second.status == "ok"
    box2 = make_box(monkeypatch=monkeypatch)  # 内容类错误不重试
    with pytest.raises(ValueError):
        agent._round({"messages": []}, box2, Replies(ValueError("bad"), AIMessage(content="")))
    assert box2.budget.used_model_requests == 1


def test_model_request_cap_stops_the_loop(monkeypatch):
    box = make_box(budget=Budget(model_requests=3), monkeypatch=monkeypatch)
    model = Replies(*[AIMessage(content="", tool_calls=[search(f"c{i}")]) for i in range(10)])
    out = agent.act({"messages": []}, {"configurable": {"toolbox": box, "model": model}})
    assert model.calls == 3 and box.budget.used_model_requests == 3 and box.budget.model_blocked()
    assert any("模型请求次数即将用完" in m.content for m in out["messages"] if isinstance(m, HumanMessage))


def test_only_providers_with_a_verified_thinking_protocol_get_the_round_trip(monkeypatch):
    from avelis import modelcfg
    from tests.unit.test_modelcfg_safety import deepseek

    monkeypatch.setattr(modelcfg, "decrypt_secret", lambda token: "unit-test-key")
    assert isinstance(modelcfg.build_chat_model(deepseek(), "deepseek-flash", effort="low"), ReasoningRoundTripChatOpenAI)
    other = deepseek()
    other.preset, other.base_url = "custom", "https://unit.invalid/v1"
    model = modelcfg.build_chat_model(other, "deepseek-flash")
    assert type(model) is ChatOpenAI and model.max_retries == 2  # 默认值不变；分析流程按阶段传 max_retries=0


# --- F. 查询复用语义 ------------------------------------------------------------------------------

def reuse_box(monkeypatch):
    from tests.unit.test_analysis_toolbox import governed, governed_catalog

    catalog, executed = governed_catalog(monkeypatch)
    box = make_box(budget=Budget(queries=20), catalog=catalog, monkeypatch=monkeypatch)
    monkeypatch.setattr(box, "_metric_sources", lambda code: (None, None, [], {}))
    return box, executed, catalog, governed


TWO = {"metric_codes": ["m_a"], "start_date": "2026-09-01", "end_date": "2026-09-30", "group_by": ["地区"],
       "compare": "custom", "compare_start": "2026-08-01", "compare_end": "2026-08-31", "purpose": "按地区两期"}


def ask(box, **changes):
    return json.loads(box.dispatch("query_metric", {**TWO, **changes}))


def test_scan_reuse_only_when_the_request_semantics_match(monkeypatch):
    from avelis.analysis.tools import QueryArgs, _scan_key

    box, executed, _, governed = reuse_box(monkeypatch)
    scan = governed(box, ["地区", "指标甲", "指标甲·对比期"], [["乙", "5", "6"], ["甲", "10", "8"]])
    scan.meta["versions"] = {"m_a": "v1"}
    box._scanned[_scan_key(QueryArgs(**TWO))] = scan
    same = ask(box, filters=[], purpose="换个说法")
    assert same["dataset"] == "D1" and same["reused"] and executed == []
    # 每一种语义变化都要重新查询（executed 记下真正执行的查询意图），不能返回 D1
    variants = {"排序": {"order": "desc"}, "行数": {"limit": 1}, "上一期": {"compare": "previous_period"},
                "只要本期": {"compare": None, "compare_start": None, "compare_end": None},
                "另一对比期": {"compare_start": "2026-07-01", "compare_end": "2026-07-31"},
                "按日": {"time_grain": "day", "compare": None, "compare_start": None, "compare_end": None},
                "筛选": {"filters": [{"dimension": "地区", "values": ["甲"]}]}}
    for label, change in variants.items():
        before = len(executed)
        out = ask(box, **change)
        assert out.get("dataset") != "D1" and len(executed) == before + 1, label
        intent = executed[-1]
        for field, value in change.items():
            got = getattr(intent, field)
            assert (got.isoformat() if hasattr(got, "isoformat") else got) == value or (
                field == "filters" and [f.model_dump() for f in got] == value), (label, field)


def test_column_order_and_metric_order_are_part_of_the_request(monkeypatch):
    from avelis.analysis.tools import QueryArgs, _scan_key

    a = QueryArgs(**{**TWO, "group_by": ["地区", "渠道"]})
    b = QueryArgs(**{**TWO, "group_by": ["渠道", "地区"]})
    assert _scan_key(a) != _scan_key(b)
    assert _scan_key(QueryArgs(**{**TWO, "metric_codes": ["m_a", "m_b"]})) != _scan_key(
        QueryArgs(**{**TWO, "metric_codes": ["m_b", "m_a"]}))
    # 筛选的先后与取值的重复不改变语义：同一个键
    f1 = [{"dimension": "地区", "values": ["甲", "乙"]}, {"dimension": "渠道", "values": ["线上"]}]
    f2 = [{"dimension": "渠道", "values": ["线上", "线上"]}, {"dimension": "地区", "values": ["乙", "甲"]}]
    assert _scan_key(QueryArgs(**{**TWO, "filters": f1})) == _scan_key(QueryArgs(**{**TWO, "filters": f2}))


def test_identical_query_is_not_reused_after_a_version_change_or_revocation(monkeypatch):
    box, executed, catalog, _ = reuse_box(monkeypatch)
    first = ask(box)
    assert ask(box)["dataset"] == first["dataset"] == "D1" and len(executed) == 1
    catalog.versions["m_a"][1].id = "v2"  # 口径发布了新版本：旧数据集按旧版本算出，不能当作当前结果
    fresh = ask(box)
    assert fresh["dataset"] == "D2" and len(executed) == 2
    assert box.datasets()["D2"].meta["versions"] == {"m_a": "v2"}

    def revoked(code):
        box._deny("没有权限读取指标的来源", metric_code=code)
    monkeypatch.setattr(box, "_metric_sources", revoked)
    denied = ask(box)
    assert denied["status"] == "denied" and "rows" not in denied and len(executed) == 2


def test_token_totals_are_recorded_and_the_cap_applies_only_when_set(monkeypatch):
    reply = AIMessage(content="", tool_calls=[], usage_metadata={"input_tokens": 900, "output_tokens": 50,
                                                                  "total_tokens": 950})
    box = make_box(monkeypatch=monkeypatch)
    agent._round({"messages": []}, box, Replies(reply))
    used = box.budget.view()["used"]
    assert (used["input_tokens"], used["output_tokens"]) == (900, 50) and box.budget.model_blocked() is None
    capped = make_box(budget=Budget(input_tokens=900), monkeypatch=monkeypatch)
    agent._round({"messages": []}, capped, Replies(reply))
    assert "token" in capped.budget.model_blocked() and not agent._can_call_model(capped)
