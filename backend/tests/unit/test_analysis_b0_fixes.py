"""共同基线 B0 的底座修复（docs/archive/agent-v2/diagnosis.md 第 4 节）：每项断言实际的状态与数据，不以返回 200 或出现关键字为据。

A. 理解阶段写了追问（Scope.clarification）或选了候选以外的指标：运行停在待澄清，保存可恢复的上下文，不继续预检与推理；
   恢复时预算接着计数。
E. 模型接入按提供方官方协议：reasoning_content 原样往返（HTTP 层用伪造的提供方核对）、截断与编号问题按基础设施错误、
   重试计入模型请求预算。
F. 查询复用不改变请求语义：排序、行数、列顺序、对比期、按日 / 按月、筛选、权限与口径版本变化时都不复用已有数据集。
"""

from __future__ import annotations

import builtins
import json
import runpy
import ssl
from types import SimpleNamespace

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models.base import OpenAITimeoutError
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import BaseModel, Field, create_model

from avelis.analysis import agent, service
from avelis.analysis.tools import Budget
from avelis.llm_protocol import ModelProtocolError, ModelTruncated, ReasoningRoundTripChatOpenAI
from tests.unit.test_analysis_pipeline import WINDOW, Phased, catalog, proposal_reply
from tests.unit.test_analysis_toolbox import make_box

CONTEXT = {"today": "2026-10-30", "tz": "UTC", "window": WINDOW}


def scope_reply(**scope):
    return AIMessage(content="", tool_calls=[{"name": "submit_scope", "args": {"question_type": "change", **scope},
                                              "id": "s1"}])


def run_graph(box, model, question="地区甲的甲量为什么下降？", *, unattended=False):
    graph = agent.build_graph(MemorySaver())
    config = {"configurable": {"thread_id": "t1", "toolbox": box, "model": model}}
    graph.invoke(agent.initial_state(question, "2026-10-30", "UTC", WINDOW, unattended=unattended), config)
    return graph.get_state(config).values


# --- A. 澄清 ---------------------------------------------------------------------------------------

def test_clarification_stops_the_run_before_precheck_and_reasoning(monkeypatch):
    box = make_box(budget=Budget(), catalog=catalog(), monkeypatch=monkeypatch)
    box.run.window, box.run.question = dict(WINDOW), "地区甲的甲量为什么下降？"
    monkeypatch.setattr(box, "precheck", lambda scope: pytest.fail("待澄清时不应执行预检"))
    model = Phased([scope_reply(metric_codes=["m_a"], filters=[{"dimension": "地区", "values": ["甲"]}],
                                clarification="“甲量”是指件数还是金额？")])
    state = run_graph(box, model, unattended=True)
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
    state = run_graph(box, Phased([scope_reply(metric_codes=["m_a", "m_zz"])]), unattended=True)
    assert state["phase"] == "clarify"
    assert box.plan["scope"]["metric_codes"] == ["m_a", "m_zz"]  # 原样保存，不删
    assert "m_zz" in box.clarification["question"] and "不在候选口径中：m_zz" in box.plan["problem"]


def test_scope_without_clarification_still_runs_the_precheck(monkeypatch):
    box = make_box(budget=Budget(), catalog=catalog(), monkeypatch=monkeypatch)
    seen = []
    monkeypatch.setattr(box, "precheck", lambda scope: seen.append(scope.metric_codes) or [])
    monkeypatch.setattr(agent.service, "version_sources", lambda session, version: {})  # 冻结目录不带来源资产。
    model = Phased([scope_reply(metric_codes=["m_a"], clarification="  "), proposal_reply()])

    def act(state, config):
        box.report = {"title": "中性结果", "summary": "证据不足", "claims": []}
        return {"messages": [], "phase": "verify", "rounds": 1, "notes": []}

    monkeypatch.setattr(agent, "act", act)
    monkeypatch.setattr(agent, "verify", lambda state, config: {"phase": "done"})
    state = run_graph(box, model)
    assert seen == [["m_a"]] and box.clarification is None and state["phase"] == "done"
    assert model.tool_choice == ["submit_scope", "submit_proposal"]
    assert box.budget.used_model_requests == 2


def test_budget_restore_keeps_counting_after_clarification():
    budget = Budget(tool_calls=30, queries=20, seconds=300, explore=8)
    budget.used_tools, budget.reports_total, budget.used_calls = 5, 1, 7
    budget.used_queries, budget.used_explore = 4, 2
    view = budget.view()
    view["used"]["seconds"] = 40.0
    restored = Budget.restore(view)
    assert restored.view()["used"] | {"seconds": 40.0} == view["used"]  # 已用次数原样恢复，不清零
    assert (restored.used_queries, restored.used_explore, restored.reports_total) == (4, 2, 1)
    # 已用的 40 秒从截止时间里扣除，等待用户的时间不计入；下限留几秒余量，全量测试负载高时 restore 到读取之间会过去一两秒
    assert 255 <= restored.remaining() <= 260
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
        self.seen = []

    def bind_tools(self, tools, **kwargs):
        return self

    def invoke(self, messages):
        self.calls += 1
        self.seen.append(list(messages))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def model_steps(box):
    return [s for s in box.session.added if getattr(s, "kind", None) == "model"]


def search(call_id, keyword="x"):
    return {"name": "search_metrics", "args": {"keyword": keyword}, "id": call_id}


def test_reasoning_is_only_displayed_in_model_step_and_keeps_protocol_content(monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    reply = AIMessage(content="", additional_kwargs={"reasoning_content": THOUGHT}, tool_calls=[search("c1")])
    out = agent._round({"messages": []}, box, Replies(reply))
    assert out[0].additional_kwargs["reasoning_content"] == THOUGHT  # 留在消息里，供下一轮回传
    assert model_steps(box)[0].output["thinking"] == THOUGHT
    recorded = json.dumps([[s.args, {k: v for k, v in s.output.items() if k != "thinking"}]
                           for s in box.session.added if hasattr(s, "output")], ensure_ascii=False)
    assert THOUGHT not in recorded


@pytest.mark.parametrize("reason_key, reason", [("finish_reason", "length"), ("stop_reason", "max_tokens")])
def test_truncated_reply_is_an_infrastructure_error_not_model_content(monkeypatch, reason_key, reason):
    box = make_box(monkeypatch=monkeypatch)

    def cut():
        return AIMessage(content="", response_metadata={reason_key: reason}, tool_calls=[search("c1")])
    with pytest.raises(ModelTruncated):
        agent._round({"messages": []}, box, Replies(cut()))
    assert box.budget.used_calls == 0  # 截断的工具调用不执行
    step = model_steps(box)[-1]
    assert step.status == "error" and step.output["error_code"] == "model_truncated"
    assert step.output["finish_reason"] == reason
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
    box = make_box(monkeypatch=monkeypatch)
    model = Replies(OpenAITimeoutError(request=httpx.Request("POST", "https://x")),
                    AIMessage(content="", tool_calls=[]))
    agent._round({"messages": []}, box, model)
    assert model.calls == 2 and box.budget.used_model_requests == 2
    first, second = model_steps(box)
    assert first.status == "error" and first.output["error_code"] == "model_transport"
    assert first.output["retry"] is True and second.status == "ok"


def wrapped_ssl_error(*, context=False):
    wrapped = Exception("包装后的连接错误")
    inner = ssl.SSLError("TLS 连接中断")
    if context:
        wrapped.__context__ = inner
        inner.__context__ = wrapped  # 包装链里存在环，也不能漏掉传输错误或无限遍历
    else:
        wrapped.__cause__ = inner
    return wrapped


@pytest.mark.parametrize("error", [
    pytest.param(openai.APIConnectionError(request=httpx.Request("POST", "https://x")), id="openai-connection"),
    pytest.param(httpx.ReadTimeout("读取超时"), id="httpx-read-timeout"),
    pytest.param(wrapped_ssl_error(), id="ssl-cause"),
    pytest.param(wrapped_ssl_error(context=True), id="ssl-context-cycle"),
])
def test_real_transport_errors_retry_once(monkeypatch, error):
    box = make_box(monkeypatch=monkeypatch)
    model = Replies(error, AIMessage(content="完成"))
    agent._round({"messages": []}, box, model)
    assert model.calls == box.budget.used_model_requests == 2
    assert [s.status for s in model_steps(box)] == ["error", "ok"]
    assert model_steps(box)[0].output["retry"] is True
    assert model_steps(box)[0].output["error_code"] == "model_transport"


@pytest.mark.parametrize("error_name, status", [
    ("RateLimitError", 429), ("InternalServerError", 500),
    ("APIConnectionError", None), ("APITimeoutError", None),
])
def test_anthropic_transient_errors_retry_once(monkeypatch, error_name, status):
    anthropic = pytest.importorskip("anthropic")
    request = httpx.Request("POST", "https://x")
    cls = getattr(anthropic, error_name)
    error = (cls("临时错误", response=httpx.Response(status, request=request), body={})
             if status else cls(request=request))
    box = make_box(monkeypatch=monkeypatch)
    model = Replies(error, AIMessage(content="完成"))
    agent._round({"messages": []}, box, model)
    assert model.calls == box.budget.used_model_requests == 2
    assert [s.status for s in model_steps(box)] == ["error", "ok"]
    assert model_steps(box)[0].output["retry"] is True
    assert model_steps(box)[0].output["error_code"] == "model_transport"


def test_anthropic_bad_request_is_not_retried(monkeypatch):
    anthropic = pytest.importorskip("anthropic")
    error = anthropic.BadRequestError("参数错误", response=httpx.Response(
        400, request=httpx.Request("POST", "https://x")), body={})
    box = make_box(monkeypatch=monkeypatch)
    model = Replies(error, AIMessage(content="不应请求"))
    with pytest.raises(anthropic.BadRequestError) as caught:
        agent._round({"messages": []}, box, model)
    assert caught.value is error
    assert model.calls == box.budget.used_model_requests == 1
    assert len(model_steps(box)) == 1 and model_steps(box)[0].output["retry"] is False
    assert "error_code" not in model_steps(box)[0].output


def test_agent_can_import_without_anthropic(monkeypatch):
    original_import = builtins.__import__

    def without_anthropic(name, *args, **kwargs):
        if name == "anthropic":
            raise ImportError("未安装可选依赖 anthropic")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_anthropic)
    module = runpy.run_path(agent.__file__)
    assert openai.APIConnectionError in module["TRANSIENT_ERRORS"]
    assert all(not cls.__module__.startswith("anthropic") for cls in module["TRANSIENT_ERRORS"])


@pytest.mark.parametrize("error", [
    pytest.param(openai.BadRequestError("参数错误", response=httpx.Response(
        400, request=httpx.Request("POST", "https://x")), body={}), id="bad-request"),
    pytest.param(openai.AuthenticationError("鉴权失败", response=httpx.Response(
        401, request=httpx.Request("POST", "https://x")), body={}), id="authentication"),
    pytest.param(ValueError("内容错误"), id="value-error"),
    pytest.param(ModelProtocolError("model_protocol", "协议错误"), id="model-protocol"),
])
def test_non_transport_errors_are_not_retried(monkeypatch, error):
    box = make_box(monkeypatch=monkeypatch)
    model = Replies(error, AIMessage(content="不应请求"))
    with pytest.raises(type(error)) as caught:
        agent._round({"messages": []}, box, model)
    assert caught.value is error
    assert model.calls == box.budget.used_model_requests == 1
    assert len(model_steps(box)) == 1 and model_steps(box)[0].output["retry"] is False
    assert "error_code" not in model_steps(box)[0].output


def test_an_unrelated_error_with_a_transport_class_name_is_not_retried(monkeypatch):
    class APIConnectionError(Exception):
        pass

    box = make_box(monkeypatch=monkeypatch)
    model = Replies(APIConnectionError("只是同名"), AIMessage(content="不应请求"))
    with pytest.raises(APIConnectionError):
        agent._round({"messages": []}, box, model)
    assert model.calls == box.budget.used_model_requests == 1
    assert model_steps(box)[0].output["retry"] is False
    assert "error_code" not in model_steps(box)[0].output


@pytest.mark.parametrize("limit", ["requests", "time", "stopped", "input_tokens", "output_tokens"])
def test_transport_retries_respect_the_remaining_run_budget(monkeypatch, limit):
    budget = Budget(model_requests=1 if limit == "requests" else 16)
    box = make_box(budget=budget, monkeypatch=monkeypatch)
    error = openai.APIConnectionError(request=httpx.Request("POST", "https://x"))
    model = Replies(error, AIMessage(content="不应请求"))
    original_invoke = model.invoke

    def fail_and_reach_limit(messages):
        if limit == "stopped":
            box.permission_lost = "发起人已失去权限"
        elif limit == "input_tokens":
            budget.used_input_tokens = budget.input_tokens
        elif limit == "output_tokens":
            budget.used_output_tokens = budget.output_tokens
        return original_invoke(messages)

    model.invoke = fail_and_reach_limit
    if limit == "time":
        monkeypatch.setattr(budget, "remaining", lambda: 2 * agent.MODEL_TIMEOUT_SECONDS)
    with pytest.raises(openai.APIConnectionError):
        agent._round({"messages": []}, box, model)
    assert model.calls == budget.used_model_requests == 1
    assert len(model_steps(box)) == 1 and model_steps(box)[0].output["retry"] is False
    assert model_steps(box)[0].output["error_code"] == "model_transport"


@pytest.mark.parametrize("restored", [False, True], ids=["same-run", "resumed-run"])
@pytest.mark.parametrize("model_requests", [3, 4], ids=["retry-budget-exhausted", "retry-budget-available"])
def test_each_model_request_can_retry_once_within_the_run_budget(monkeypatch, restored, model_requests):
    box = make_box(budget=Budget(model_requests=model_requests), monkeypatch=monkeypatch)
    error = httpx.ReadTimeout("再次超时")
    model = Replies(openai.APIConnectionError(request=httpx.Request("POST", "https://x")),
                    AIMessage(content="第一轮成功"), error, AIMessage(content="第二轮成功"))
    agent._round({"messages": []}, box, model)
    assert model.calls == box.budget.used_model_requests == 2
    if restored:
        box.budget = Budget.restore(box.budget.view())
    if model_requests == 3:
        with pytest.raises(httpx.ReadTimeout) as caught:
            agent._round({"messages": []}, box, model)
        assert caught.value is error
        assert [s.output.get("retry") for s in model_steps(box)] == [True, None, False]
    else:
        agent._round({"messages": []}, box, model)
        assert [s.status for s in model_steps(box)] == ["error", "ok", "error", "ok"]
        assert [s.output.get("retry") for s in model_steps(box)] == [True, None, True, None]
    assert model.calls == box.budget.used_model_requests == model_requests
    assert box.budget.model_blocked()


def test_consecutive_transport_errors_in_one_request_retry_only_once(monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    error = httpx.ReadTimeout("重试仍超时")
    model = Replies(openai.APIConnectionError(request=httpx.Request("POST", "https://x")),
                    error, AIMessage(content="不应请求"))
    with pytest.raises(httpx.ReadTimeout) as caught:
        agent._round({"messages": []}, box, model)
    assert caught.value is error and model.calls == box.budget.used_model_requests == 2
    assert [s.status for s in model_steps(box)] == ["error", "error"]
    assert [s.output["retry"] for s in model_steps(box)] == [True, False]
    assert all(s.output["error_code"] == "model_transport" for s in model_steps(box))


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
    assert (Budget().input_tokens, Budget().output_tokens) == (552_321, 30_716)  # E1 前统一修订的冻结值
    box = make_box(budget=Budget(input_tokens=None, output_tokens=None), monkeypatch=monkeypatch)
    agent._round({"messages": []}, box, Replies(reply))
    used = box.budget.view()["used"]
    assert (used["input_tokens"], used["output_tokens"]) == (900, 50) and box.budget.model_blocked() is None
    capped = make_box(budget=Budget(input_tokens=900), monkeypatch=monkeypatch)
    monkeypatch.setattr(capped, "tools", lambda: [])  # 只检查实际用量到上限，不让工具定义先触发预估截止。
    agent._round({"messages": []}, capped, Replies(reply))
    assert "token" in capped.budget.model_blocked() and not agent._can_call_model(capped)


@pytest.mark.parametrize("field", ["content", "tool_calls", "reasoning_content", "tool_result"])
def test_input_budget_rejects_estimated_prefix_growth_before_the_next_request(monkeypatch, field):
    box = make_box(budget=Budget(input_tokens=100), monkeypatch=monkeypatch)
    messages = [HumanMessage("原问题")]
    reply = AIMessage(content="x" * 21 if field == "content" else "",
                      tool_calls=[search("c1", "x" * 21)] if field == "tool_calls" else [],
                      additional_kwargs={"reasoning_content": "x" * 21} if field == "reasoning_content" else {},
                      usage_metadata={"input_tokens": 40, "output_tokens": 1, "total_tokens": 41})
    model = Replies(reply, AIMessage(content="不应请求"))
    assert agent._invoke(model, messages, box) is reply
    added = [reply, *([ToolMessage(content="x" * 21, tool_call_id="c1")] if field == "tool_result" else [])]
    chars = sum(len(m.content) + (len(json.dumps(m.tool_calls, ensure_ascii=False, default=str))
                                 if getattr(m, "tool_calls", None) else 0)
                + (len(json.dumps(m.additional_kwargs, ensure_ascii=False, default=str))
                   if m.additional_kwargs else 0) for m in added)
    assert agent._round({"messages": [*messages, *added]}, box, model) == []
    assert model.calls == box.budget.used_model_requests == 1 and box.budget.used_input_tokens == 40
    assert box.budget.model_blocked() and not agent._can_call_model(box)
    step = model_steps(box)[-1]
    assert step.status == "skipped" and step.output == {
        "error_code": "budget_tokens", "estimated_input_tokens": 40 + chars,
        "used_input_tokens": 40, "input_tokens": 100,
    }


@pytest.mark.parametrize("growth", [19, 20], ids=["within-cap", "equal-to-cap"])
def test_input_estimates_within_the_remaining_budget_allow_the_request(monkeypatch, growth):
    box = make_box(budget=Budget(input_tokens=100), monkeypatch=monkeypatch)
    messages = [HumanMessage("原问题")]
    first = AIMessage(content="x" * growth,
                      usage_metadata={"input_tokens": 40, "output_tokens": 1, "total_tokens": 41})
    second = AIMessage(content="完成", usage_metadata={"input_tokens": 40 + growth, "output_tokens": 1,
                                                     "total_tokens": 41 + growth})
    model = Replies(first, second)
    agent._invoke(model, messages, box)
    out = agent._round({"messages": [*messages, first]}, box, model)
    assert out[0] is second and model.calls == box.budget.used_model_requests == 2
    assert box.budget.used_input_tokens == 80 + growth
    assert [s.status for s in model_steps(box)] == ["ok", "ok"]


def test_input_budget_cutoff_still_verifies_an_incomplete_report_with_existing_datasets(monkeypatch):
    box = make_box(budget=Budget(input_tokens=100), monkeypatch=monkeypatch)
    box.run.window = dict(WINDOW)
    dataset = box._dataset("governed", "已取得的数据", ["指标甲"], [["1"]])
    messages = [HumanMessage("原问题")]
    first = AIMessage(content="x" * 21,
                      usage_metadata={"input_tokens": 40, "output_tokens": 1, "total_tokens": 41})
    model = Replies(first, AIMessage(content="不应请求"))
    config = {"configurable": {"toolbox": box, "model": model}}
    agent._invoke(model, messages, box)
    out = agent.act({"messages": [*messages, first]}, config)
    assert out["phase"] == "verify" and model.calls == 1
    assert any("预算内没有提交报告" in note for note in out["notes"])
    assert model_steps(box)[-1].output["error_code"] == "budget_tokens"
    assert agent.verify(out, config)["phase"] == "done"
    assert box.report["incomplete"] is True and box.verification["status"] == "no_facts"
    assert "已取得的数据集" in box.report["summary"]
    assert box.datasets() == {"D1": dataset} and dataset.title == "已取得的数据"


def test_nearly_exhausted_input_tokens_prompt_for_a_report_and_block_other_tools(monkeypatch):
    box = make_box(budget=Budget(input_tokens=400), monkeypatch=monkeypatch)
    messages = [HumanMessage("原问题")]
    first = AIMessage(content="x" * 100,
                      usage_metadata={"input_tokens": 100, "output_tokens": 1, "total_tokens": 101})
    report = {"title": "证据不足", "summary": "仅基于已有数据", "claims": [],
              "conclusion": "insufficient_evidence"}
    model = Replies(first, AIMessage(content="", tool_calls=[
        search("c1"), {"name": "submit_report", "args": report, "id": "c2"},
    ]))
    agent._invoke(model, messages, box)
    estimate = box.budget.estimate_input_tokens([*messages, first])
    assert box.budget.used_input_tokens + estimate <= box.budget.input_tokens
    assert box.budget.used_input_tokens + 2 * estimate > box.budget.input_tokens
    out = agent.act({"messages": [*messages, first]}, {"configurable": {"toolbox": box, "model": model}})
    assert model.calls == 2 and box.report is not None
    notices = [m.content for m in model.seen[-1] if isinstance(m, HumanMessage)]
    assert sum("token 即将用完" in text for text in notices) == 1
    replies = {m.tool_call_id: json.loads(m.content) for m in out["messages"] if isinstance(m, ToolMessage)}
    assert "只能调用 submit_report" in replies["c1"]["error"] and replies["c2"] == {"ok": True}
    assert box.budget.used_tools == 0 and box.budget.used_reports == 1


@pytest.mark.parametrize("field", ["name", "description", "schema"])
def test_tool_definitions_without_a_reusable_prefix_block_requests_before_the_cap(monkeypatch, field):
    args = create_model("LongArgs", value=(str, Field(description="x" * 2000 if field == "schema" else "值")))
    tool = StructuredTool.from_function(lambda value: value, args_schema=args,
                                        name="x" * 2000 if field == "name" else "echo",
                                        description="x" * 2000 if field == "description" else "回声")
    box = make_box(budget=Budget(input_tokens=10_000), monkeypatch=monkeypatch)
    box.budget.used_input_tokens = 9000
    monkeypatch.setattr(box, "tools", lambda: [tool])
    messages = [HumanMessage("短问题")]
    assert box.budget.estimate_input_tokens(messages) == len(messages[0].content)
    estimate = box.budget.estimate_input_tokens(messages, [tool])
    assert estimate >= (len(messages[0].content) + len(tool.name) + len(tool.description)
                        + len(json.dumps(args.model_json_schema(), ensure_ascii=False)))
    model = Replies()
    assert agent._round({"messages": messages}, box, model) == []
    assert model.calls == box.budget.used_model_requests == 0 and box.budget.used_input_tokens == 9000
    step = model_steps(box)[0]
    assert step.status == "skipped" and step.output == {
        "error_code": "budget_tokens", "estimated_input_tokens": estimate,
        "used_input_tokens": 9000, "input_tokens": 10_000,
    }


def test_tool_definitions_are_not_counted_again_with_a_reusable_prefix(monkeypatch):
    tool = StructuredTool.from_function(lambda value: value, name="echo", args_schema=_Args, description="x" * 2000)
    box = make_box(budget=Budget(input_tokens=1000), monkeypatch=monkeypatch)
    monkeypatch.setattr(box, "tools", lambda: [tool])
    prefix = [HumanMessage("旧问题")]
    box.budget.used_input_tokens = 900
    box.budget.remember_model_input(prefix, 40)
    added = AIMessage(content="新回复")
    estimate = 40 + len(added.content)
    assert box.budget.estimate_input_tokens([*prefix, added], [tool]) == estimate
    reply = AIMessage(content="完成", usage_metadata={"input_tokens": estimate, "output_tokens": 1,
                                                   "total_tokens": estimate + 1})
    model = Replies(reply)
    assert agent._round({"messages": [*prefix, added]}, box, model)[0] is reply
    assert model.calls == box.budget.used_model_requests == 1
    assert box.budget.used_input_tokens == 900 + estimate and model_steps(box)[0].status == "ok"


def test_different_conversations_and_restored_budgets_estimate_all_messages_and_tools():
    budget = Budget(input_tokens=1000)
    tool = StructuredTool.from_function(lambda value: value, name="echo", args_schema=_Args, description="工具定义" * 10)
    prefix = [HumanMessage("旧问题" * 10)]
    added = AIMessage(content="新回复")
    expected = budget.estimate_input_tokens([*prefix, added], [tool])
    assert expected > len(prefix[0].content) + len(added.content) + len(tool.description)
    budget.used_input_tokens = 7
    budget.remember_model_input(prefix, 7)
    assert budget.estimate_input_tokens([*prefix, added], [tool]) == 7 + len(added.content)
    different = [HumanMessage(prefix[0].content), added]  # 内容相同但不是原消息，也不能沿用旧对话的实际用量
    assert budget.estimate_input_tokens(different, [tool]) == expected
    restored = Budget.restore(budget.view())
    assert restored.used_input_tokens == 7
    assert restored.estimate_input_tokens([*prefix, added], [tool]) == expected


def test_unlimited_input_tokens_neither_estimate_nor_block_requests(monkeypatch):
    budget = Budget(input_tokens=None)
    budget.used_input_tokens = 10**9
    box = make_box(budget=budget, monkeypatch=monkeypatch)
    messages = [HumanMessage("原问题" * 1000)]
    assert budget.estimate_input_tokens(messages) is None
    monkeypatch.setattr(budget, "estimate_input_tokens", lambda messages: pytest.fail("不限输入时不应预估"))
    report = {"title": "证据不足", "summary": "已有数据不足", "claims": [],
              "conclusion": "insufficient_evidence"}
    model = Replies(AIMessage(content="完成"), AIMessage(content="", tool_calls=[
        {"name": "submit_report", "args": report, "id": "c1"},
    ]))
    agent._round({"messages": messages}, box, model)
    agent.act({"messages": messages}, {"configurable": {"toolbox": box, "model": model}})
    assert model.calls == box.budget.used_model_requests == 2 and box.report is not None
    assert budget.model_blocked() is None and [s.status for s in model_steps(box)] == ["ok", "ok"]


def test_input_cutoff_during_understanding_finishes_the_graph_normally(monkeypatch):
    box = make_box(budget=Budget(input_tokens=1000), catalog=catalog(), monkeypatch=monkeypatch)
    box.budget.used_input_tokens = 900
    box.run.window = dict(WINDOW)
    # 缩短正文，剩余额度足够放消息，只有 submit_scope 的工具定义会触发输入截止。
    monkeypatch.setattr(agent, "UNDERSTAND_PROMPT", "")
    monkeypatch.setattr(box, "candidate_metrics", lambda question: [])
    model = Replies()
    state = run_graph(box, model, question="q")
    assert state["phase"] == "done" and model.calls == box.budget.used_model_requests == 0
    assert box.report["incomplete"] is True and box.verification["status"] == "no_facts"
    assert len(model_steps(box)) == 1 and model_steps(box)[0].output["error_code"] == "budget_tokens"
    assert not any("理解阶段失败" in note for note in state["notes"])


@pytest.mark.parametrize("exhausted", ["requests", "input_tokens", "output_tokens"])
def test_last_request_report_and_verification_survive_exhausted_model_budget(monkeypatch, exhausted):
    """修订前已无模型额度时，保留最后一次请求提交的报告及其核验问题（PR #11，4426619628）。"""
    budget = Budget(model_requests=1 if exhausted == "requests" else 2,
                    input_tokens=100_000 if exhausted == "input_tokens" else None,
                    output_tokens=100 if exhausted == "output_tokens" else None)
    box = make_box(budget=budget, monkeypatch=monkeypatch)
    box.run.window = dict(WINDOW)
    report = {"title": "已提交报告", "summary": "已取得 42 条数据", "claims": [],
              "conclusion": "insufficient_evidence"}
    used_input = 100_000 if exhausted == "input_tokens" else 1
    used_output = 100 if exhausted == "output_tokens" else 1
    reply = AIMessage(content="", tool_calls=[{"name": "submit_report", "args": report, "id": "c1"}],
                      usage_metadata={"input_tokens": used_input, "output_tokens": used_output,
                                      "total_tokens": used_input + used_output})
    model = Replies(reply)
    config = {"configurable": {"toolbox": box, "model": model}}
    state = agent.reason({"question": "原问题", "context": CONTEXT}, config)
    assert state["phase"] == "done" and state["revised"] is False
    assert model.calls == budget.used_model_requests == 1 and budget.model_blocked()
    assert box.report["title"] == report["title"] and box.report["summary"] == report["summary"]
    assert box.report["claims"] == [] and not box.report.get("incomplete")
    assert box.verification["ok"] is False and box.verification["status"] == "failed"
    assert box.verification["revised"] is False
    summary = next(field for field in box.verification["fields"] if field["field"] == "summary")
    assert summary["status"] == "unverified" and any("42" in problem for problem in summary["problems"])
    assert budget.tool_calls == Budget().tool_calls and budget.reports_total == 1


def test_report_with_verification_problems_is_revised_when_model_budget_remains(monkeypatch):
    box = make_box(budget=Budget(model_requests=2, input_tokens=None, output_tokens=None), monkeypatch=monkeypatch)
    box.run.window = dict(WINDOW)
    report = {"title": "已提交报告", "summary": "已取得 42 条数据", "claims": [],
              "conclusion": "insufficient_evidence"}
    corrected = {**report, "summary": "已有数据不足"}
    model = Replies(*[AIMessage(content="", tool_calls=[{"name": "submit_report", "args": args, "id": call_id}])
                      for args, call_id in ((report, "c1"), (corrected, "c2"))])
    state = agent.reason({"question": "原问题", "context": CONTEXT},
                         {"configurable": {"toolbox": box, "model": model}})
    assert state["phase"] == "done" and state["revised"] is True
    assert model.calls == box.budget.used_model_requests == 2 and box.budget.reports_total == 2
    assert box.report["summary"] == corrected["summary"] and not box.report.get("incomplete")
    assert box.verification["ok"] is True and box.verification["revised"] is True
    assert any("核验发现以下问题" in message.content for message in model.seen[1] if isinstance(message, HumanMessage))


def test_input_cutoff_during_revision_uses_the_same_incomplete_report_path(monkeypatch):
    box = make_box(budget=Budget(input_tokens=10), monkeypatch=monkeypatch)
    box.run.window = dict(WINDOW)
    box.report = {"title": "待修订", "summary": "证据不足", "claims": []}
    box.verification = {"claims": [], "fields": []}
    model = Replies()
    config = {"configurable": {"toolbox": box, "model": model}}
    revision = agent.revise({}, config)
    out = agent.act({"messages": [HumanMessage("原问题"), *revision["messages"]]}, config)
    assert out["phase"] == "verify" and model.calls == box.budget.used_model_requests == 0
    assert model_steps(box)[-1].output["error_code"] == "budget_tokens"
    assert agent.verify({"revised": True}, config)["phase"] == "done"
    assert box.report["incomplete"] is True and box.verification["revised"] is True


def test_deepseek_output_limit_uses_max_tokens_on_the_wire():
    for cls, expected, unexpected in (
        (ReasoningRoundTripChatOpenAI, "max_tokens", "max_completion_tokens"),
        (ChatOpenAI, "max_completion_tokens", "max_tokens"),
    ):
        bodies: list[dict] = []
        model = cls(model="deepseek-flash", api_key="unit-test-key", base_url="https://deepseek.invalid",
                    http_client=fake_deepseek(bodies), max_tokens=8, max_retries=0)
        model.invoke([HumanMessage("调用 echo")])
        assert len(bodies) == 1 and bodies[0][expected] == 8
        assert unexpected not in bodies[0]


@pytest.mark.parametrize("limit, used, remaining", [
    (100, 0, 100), (100, 27, 73), (100, 100, 0), (100, 120, 0), (None, 10**9, None),
])
def test_remaining_output_tokens_is_a_nonnegative_pure_budget_view(limit, used, remaining):
    budget = Budget(output_tokens=limit)
    budget.used_output_tokens = used
    assert budget.remaining_output_tokens() == remaining
    assert budget.used_output_tokens == used and budget.output_tokens == limit


@pytest.mark.parametrize("limit, used, expected", [(40, 7, 33), (2000, 0, 1000)])
def test_understanding_requests_the_smaller_of_phase_and_remaining_output_tokens(monkeypatch, limit, used, expected):
    box = make_box(budget=Budget(output_tokens=limit), catalog=catalog(), monkeypatch=monkeypatch)
    box.budget.used_output_tokens = used
    model = Phased([scope_reply(metric_codes=["m_a"])])
    out = agent.understand({"question": "问题", "context": CONTEXT},
                           {"configurable": {"toolbox": box, "model": model}})
    assert model.phases == [{"effort": "off", "max_tokens": expected}]
    assert len(model.seen) == 1 and box.budget.used_model_requests == 1
    assert out["phase"] == "precheck" and out["scope"]["metric_codes"] == ["m_a"]


@pytest.mark.parametrize("restored", [False, True], ids=["same-run", "resumed-run"])
def test_reasoning_output_caps_follow_actual_usage_between_requests(monkeypatch, restored):
    box = make_box(budget=Budget(output_tokens=40), monkeypatch=monkeypatch)
    box.budget.used_output_tokens = 2
    first = AIMessage(content="第一轮", usage_metadata={"input_tokens": 10, "output_tokens": 7, "total_tokens": 17})
    second = AIMessage(content="第二轮", usage_metadata={"input_tokens": 12, "output_tokens": 3, "total_tokens": 15})
    model = Phased([first, second])
    assert agent._round({"messages": []}, box, model)[0] is first
    if restored:
        box.budget = Budget.restore(box.budget.view())
    assert agent._round({"messages": [first]}, box, model)[0] is second
    assert model.phases == [{"max_tokens": 38}, {"max_tokens": 31}]
    assert box.budget.used_output_tokens == 12 and box.budget.remaining_output_tokens() == 28
    assert [s.output["output_tokens"] for s in model_steps(box)] == [7, 3]


@pytest.mark.parametrize("remaining", [13, 5000, 8191])
@pytest.mark.parametrize("output_used", [None, 0, 4], ids=["usage-missing", "zero-usage", "below-request-cap"])
@pytest.mark.parametrize("reason_key, reason", [("finish_reason", "length"), ("stop_reason", "max_tokens")])
def test_budget_limited_length_discards_content_and_tools_and_blocks_the_next_request(
        monkeypatch, remaining, output_used, reason_key, reason):
    box = make_box(budget=Budget(output_tokens=remaining + 2), monkeypatch=monkeypatch)
    box.budget.used_output_tokens = 2
    usage = ({"input_tokens": 10, "output_tokens": output_used, "total_tokens": 10 + output_used}
             if output_used is not None else None)
    reply = AIMessage(content="截断的内容不能继续使用", tool_calls=[search("c1")],
                      response_metadata={reason_key: reason}, usage_metadata=usage)
    model = Phased([reply, AIMessage(content="不应请求")])
    assert agent._round({"messages": []}, box, model) == []
    assert model.phases == [{"max_tokens": remaining}] and len(model.seen) == 1
    assert box.budget.used_output_tokens == 2 + (output_used or 0)  # 没有伪造已用量来阻断下一次请求。
    assert box.budget.used_calls == 0 and box.budget.model_blocked()
    step = model_steps(box)[0]
    assert step.status == "error" and step.output["error_code"] == "budget_output_tokens"
    assert step.output["output_tokens"] == output_used and step.output["finish_reason"] == reason
    assert agent._round({"messages": []}, box, model) == []
    assert len(model.seen) == box.budget.used_model_requests == 1 and len(model_steps(box)) == 1


@pytest.mark.parametrize("remaining, output_used", [
    (30_000, None), (30_000, 23_000), (8192, None), (None, None),
], ids=["ample-budget", "usage-crosses-ceiling", "equal-to-ceiling", "unlimited-output"])
@pytest.mark.parametrize("reason_key, reason", [("finish_reason", "length"), ("stop_reason", "max_tokens")])
def test_reasoning_without_a_budget_cap_still_raises_model_truncated(
        monkeypatch, remaining, output_used, reason_key, reason):
    box = make_box(budget=Budget(output_tokens=remaining), monkeypatch=monkeypatch)
    usage = ({"input_tokens": 10, "output_tokens": output_used, "total_tokens": 10 + output_used}
             if output_used is not None else None)
    reply = AIMessage(content="提供方默认上限造成的截断", tool_calls=[search("c1")],
                      response_metadata={reason_key: reason}, usage_metadata=usage)
    model = Phased([reply])
    with pytest.raises(ModelTruncated):
        agent._round({"messages": []}, box, model)
    assert model.phases == [{}] and len(model.seen) == box.budget.used_model_requests == 1
    assert box.budget.used_calls == 0 and box.budget.used_output_tokens == (output_used or 0)
    assert box.budget.model_blocked() is None
    step = model_steps(box)[0]
    assert step.status == "error" and step.output["error_code"] == "model_truncated"
    assert step.output["output_tokens"] == output_used and step.output["finish_reason"] == reason


@pytest.mark.parametrize("limit", [agent.UNDERSTAND_MAX_TOKENS, 30_000, None],
                         ids=["equal-to-phase-cap", "phase-cap-is-smaller", "unlimited-output"])
@pytest.mark.parametrize("reason_key, reason", [("finish_reason", "length"), ("stop_reason", "max_tokens")])
def test_understanding_phase_limit_still_raises_model_truncated(monkeypatch, limit, reason_key, reason):
    box = make_box(budget=Budget(output_tokens=limit), monkeypatch=monkeypatch)
    model = Phased([AIMessage(content="截断", response_metadata={reason_key: reason})])
    with pytest.raises(ModelTruncated):
        agent.understand({"question": "问题", "context": CONTEXT},
                         {"configurable": {"toolbox": box, "model": model}})
    assert model.phases == [{"effort": "off", "max_tokens": agent.UNDERSTAND_MAX_TOKENS}]
    assert model_steps(box)[0].output["error_code"] == "model_truncated"
    assert model_steps(box)[0].output["finish_reason"] == reason
    assert box.budget.model_blocked() is None


@pytest.mark.parametrize("reason_key, reason", [("finish_reason", "length"), ("stop_reason", "max_tokens")])
def test_length_without_a_factory_is_not_treated_as_a_budget_limited_request(monkeypatch, reason_key, reason):
    box = make_box(budget=Budget(output_tokens=10), monkeypatch=monkeypatch)
    model = Replies(AIMessage(content="截断", response_metadata={reason_key: reason}))
    with pytest.raises(ModelTruncated):
        agent._round({"messages": []}, box, model)
    assert model.calls == 1 and model_steps(box)[0].output["error_code"] == "model_truncated"
    assert box.budget.used_output_tokens == 0 and box.budget.model_blocked() is None


@pytest.mark.parametrize("stop_reason", ["end_turn", "tool_use"])
def test_anthropic_normal_stop_reasons_are_not_truncated(monkeypatch, stop_reason):
    box = make_box(budget=Budget(output_tokens=13), monkeypatch=monkeypatch)
    calls = [search("c1")] if stop_reason == "tool_use" else []
    reply = AIMessage(content="完成", response_metadata={"stop_reason": stop_reason}, tool_calls=calls)
    model = Phased([reply])
    out = agent._round({"messages": []}, box, model)
    assert out[0] is reply and box.budget.used_calls == len(calls)
    assert model.phases == [{"max_tokens": 13}] and box.budget.used_model_requests == 1
    assert box.budget.model_blocked() is None
    step = model_steps(box)[0]
    assert step.status == "ok" and step.output["finish_reason"] == stop_reason


@pytest.mark.parametrize("used", [10, 11], ids=["exhausted", "over-budget"])
def test_exhausted_output_tokens_skip_understanding_and_reasoning_before_factory(monkeypatch, used):
    box = make_box(budget=Budget(output_tokens=10), monkeypatch=monkeypatch)
    box.budget.used_output_tokens = used
    model = Phased([])
    assert agent._round({"messages": []}, box, model) == []
    out = agent.understand({"question": "问题", "context": CONTEXT},
                           {"configurable": {"toolbox": box, "model": model}})
    assert out["scope"] is None and model.phases == model.seen == []
    assert box.budget.used_model_requests == 0 and model_steps(box) == []


def test_unlimited_output_tokens_do_not_add_a_request_cap_or_block_but_keep_the_phase_cap(monkeypatch):
    box = make_box(budget=Budget(input_tokens=None, output_tokens=None), monkeypatch=monkeypatch)
    box.budget.used_output_tokens = 10**9
    first = AIMessage(content="第一轮", usage_metadata={"input_tokens": 1, "output_tokens": 5, "total_tokens": 6})
    model = Phased([first, scope_reply(metric_codes=[])])
    assert agent._round({"messages": []}, box, model)[0] is first
    agent.understand({"question": "问题", "context": CONTEXT},
                     {"configurable": {"toolbox": box, "model": model}})
    assert model.phases == [{}, {"effort": "off", "max_tokens": agent.UNDERSTAND_MAX_TOKENS}]
    assert len(model.seen) == box.budget.used_model_requests == 2
    assert box.budget.used_output_tokens == 10**9 + 5 and box.budget.model_blocked() is None


def test_output_cutoff_during_understanding_finishes_the_graph_and_preserves_existing_datasets(monkeypatch):
    box = make_box(budget=Budget(input_tokens=None, output_tokens=20), catalog=catalog(), monkeypatch=monkeypatch)
    box.run.window = dict(WINDOW)
    dataset = box._dataset("governed", "已取得的数据", ["指标甲"], [["1"]])
    reply = scope_reply(metric_codes=["m_a"], clarification="截断的追问不能执行")
    reply.response_metadata = {"finish_reason": "length"}
    model = Phased([reply, AIMessage(content="不应请求")])
    monkeypatch.setattr(box, "precheck", lambda scope: pytest.fail("截断的范围不能用于预检"))
    state = run_graph(box, model)
    assert state["phase"] == "done" and state["scope"] is None and box.clarification is None
    assert model.phases == [{"effort": "off", "max_tokens": 20}] and len(model.seen) == 1
    assert model_steps(box)[0].output["error_code"] == "budget_output_tokens"
    assert box.report["incomplete"] is True and box.verification["status"] == "no_facts"
    assert "已取得的数据集" in box.report["summary"]
    assert box.datasets() == {"D1": dataset} and dataset.title == "已取得的数据"
    assert not any("理解阶段失败" in note for note in state["notes"])


@pytest.mark.parametrize("revised", [False, True], ids=["reasoning", "revision"])
def test_output_cutoff_finishes_reasoning_and_revision_with_existing_datasets(monkeypatch, revised):
    box = make_box(budget=Budget(input_tokens=None, output_tokens=20), monkeypatch=monkeypatch)
    box.run.window = dict(WINDOW)
    dataset = box._dataset("governed", "已取得的数据", ["指标甲"], [["1"]])
    reply = AIMessage(content="截断", tool_calls=[search("c1")], response_metadata={"finish_reason": "length"})
    model = Phased([reply, AIMessage(content="不应请求")])
    config = {"configurable": {"toolbox": box, "model": model}}
    messages = [HumanMessage("原问题")]
    if revised:
        box.report = {"title": "待修订", "summary": "证据不足", "claims": []}
        box.verification = {"claims": [], "fields": []}
        messages += agent.revise({}, config)["messages"]
    out = agent.act({"messages": messages}, config)
    assert out["phase"] == "verify" and model.phases == [{"max_tokens": 20}]
    assert len(model.seen) == box.budget.used_model_requests == 1 and box.budget.used_calls == 0
    assert model_steps(box)[0].output["error_code"] == "budget_output_tokens"
    assert agent.verify({"revised": revised}, config)["phase"] == "done"
    assert box.report["incomplete"] is True and box.verification["revised"] is revised
    assert box.datasets() == {"D1": dataset} and "已取得的数据集" in box.report["summary"]


def test_transport_retry_reuses_the_same_remaining_output_cap(monkeypatch):
    box = make_box(budget=Budget(output_tokens=20), monkeypatch=monkeypatch)
    box.budget.used_output_tokens = 3
    request_caps = []

    class RetryingPhased(Phased):
        def factory(self, timeout, max_retries, **phase):
            self.cap = phase["max_tokens"]
            return super().factory(timeout, max_retries, **phase)

        def invoke(self, messages):
            request_caps.append(self.cap)
            reply = super().invoke(messages)
            if isinstance(reply, Exception):
                raise reply
            return reply

    model = RetryingPhased([httpx.ReadTimeout("首次超时"), AIMessage(content="完成")])
    assert agent._round({"messages": []}, box, model)[0].content == "完成"
    assert request_caps == [17, 17] and box.budget.used_model_requests == 2
    assert box.budget.used_output_tokens == 3 and [s.status for s in model_steps(box)] == ["error", "ok"]
