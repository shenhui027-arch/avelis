"""运行中的实时过程：流式回复按官方协议合并（推理文字、工具调用、用量、终止原因与非流式一致），
推理文字边收边交给实时缓冲；正在执行的工具写入实时缓冲；Redis 不可用时不影响分析；接口只在运行中返回。"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from avelis.analysis import agent, live
from avelis.api import analysis as analysis_api
from avelis.llm_protocol import ReasoningRoundTripChatOpenAI, response_finish_reason, stream_reply, streams_live
from tests.unit.test_analysis_toolbox import make_box


class _Args(BaseModel):
    value: str


def _sse(*events: dict) -> bytes:
    return "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events).encode() + b"data: [DONE]\n\n"


def _chunk(delta: dict | None = None, finish: str | None = None, usage: dict | None = None) -> dict:
    return {
        "id": "x",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "deepseek-flash",
        "choices": [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": finish}],
        **({"usage": usage} if usage else {}),
    }


USAGE = {
    "prompt_tokens": 120,
    "completion_tokens": 30,
    "total_tokens": 150,
    "prompt_cache_hit_tokens": 100,
    "completion_tokens_details": {"reasoning_tokens": 20},
}


def fake_stream(bodies: list[dict]) -> httpx.Client:
    """按 OpenAI 兼容流式协议伪造 DeepSeek：第一轮推理分两段到达、工具参数分片；第二轮要求回传 reasoning_content。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        assistants = [m for m in body["messages"] if m["role"] == "assistant"]
        if body.get("tools") and any("reasoning_content" not in m for m in assistants):
            return httpx.Response(400, json={"error": {"message": "reasoning_content must be passed back"}})
        if not assistants:
            events = [
                _chunk({"role": "assistant", "content": None, "reasoning_content": "先看"}),
                _chunk({"reasoning_content": "口径"}),
                _chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_a",
                                "type": "function",
                                "function": {"name": "echo", "arguments": ""},
                            }
                        ]
                    }
                ),
                _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"value": '}}]}),
                _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"a"}'}}]}),
                _chunk({}, finish="tool_calls"),
                _chunk(usage=USAGE),
            ]
        else:
            events = [
                _chunk({"role": "assistant", "content": "", "reasoning_content": "第二轮"}),
                _chunk({"content": "完"}),
                _chunk({"content": "成"}, finish="stop"),
                _chunk(usage=USAGE),
            ]
        return httpx.Response(200, content=_sse(*events), headers={"content-type": "text/event-stream"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def raw_model(bodies: list[dict]) -> ReasoningRoundTripChatOpenAI:
    return ReasoningRoundTripChatOpenAI(
        model="deepseek-flash",
        api_key="unit-test-key",
        base_url="https://deepseek.invalid",
        http_client=fake_stream(bodies),
        max_retries=0,
    )


def streaming_model(bodies: list[dict]):
    return raw_model(bodies).bind_tools(
        [StructuredTool.from_function(lambda value: value, name="echo", args_schema=_Args, description="e")]
    )


def test_stream_reply_merges_like_a_non_streaming_reply_and_round_trips_reasoning(monkeypatch):
    bodies: list[dict] = []
    model = streaming_model(bodies)
    assert streams_live(model)
    deltas: list[tuple[str, str]] = []
    first = stream_reply(model, [HumanMessage("调用 echo")], lambda t, x: deltas.append((t, x)), interval=0)
    assert bodies[0]["stream"] is True and bodies[0]["stream_options"] == {"include_usage": True}
    assert first.additional_kwargs["reasoning_content"] == "先看口径" and first.content == ""
    assert first.tool_calls == [{"name": "echo", "args": {"value": "a"}, "id": "call_a", "type": "tool_call"}]
    assert response_finish_reason(first) == "tool_calls"
    assert agent.model_usage(first) == {
        "input_tokens": 120,
        "output_tokens": 30,
        "cache_hit_tokens": 100,
        "reasoning_tokens": 20,
    }
    assert deltas[0] == ("先看", "") and deltas[-1] == ("先看口径", "")  # 累计文字，不是增量片段
    messages = [HumanMessage("调用 echo"), first, ToolMessage(content="ok", tool_call_id="call_a")]
    last = stream_reply(model, messages, lambda t, x: deltas.append((t, x)), interval=0)
    sent = [m for m in bodies[1]["messages"] if m["role"] == "assistant"][0]
    assert sent["reasoning_content"] == "先看口径"  # 流式合并出的推理文字同样原样回传
    assert last.content == "完成" and deltas[-1] == ("第二轮", "完成")


def test_stream_reply_stops_at_the_deadline():
    model = streaming_model([])
    with pytest.raises(TimeoutError):
        stream_reply(model, [HumanMessage("x")], lambda t, x: None, deadline=time.monotonic() - 1)


def test_only_verified_thinking_providers_stream():
    assert not streams_live(SimpleNamespace(invoke=lambda m: None))
    assert not streams_live(SimpleNamespace(bound=object()))


class RecordingSink:
    def __init__(self):
        self.calls: list[tuple] = []
        self.disabled = False
        self.run_id = "r1"

    def model_started(self, after_seq):
        self.calls.append(("model", after_seq))

    def model_delta(self, thinking, text, *, force=False):
        self.calls.append(("delta", thinking, text))

    def tool_started(self, after_seq, name, args):
        self.calls.append(("tool", after_seq, name, args))


def test_round_streams_thinking_and_marks_the_running_tool(monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    sink = RecordingSink()
    monkeypatch.setattr(agent, "_live_sink", lambda toolbox: sink)
    model = SimpleNamespace(model=raw_model([]))
    dispatched = []
    monkeypatch.setattr(box, "dispatch", lambda name, args, only_report=False: dispatched.append((name, args)) or "ok")
    out = agent._round({"messages": [HumanMessage("调用 echo")]}, box, model)
    assert sink.calls[0] == ("model", 0)
    assert ("delta", "先看", "") in sink.calls or ("delta", "先看口径", "") in sink.calls
    model_step = [s for s in box.session.added if getattr(s, "kind", None) == "model"][0]
    assert model_step.output["thinking"] == "先看口径" and model_step.output["tools"] == ["echo"]
    assert sink.calls[-1] == ("tool", model_step.seq, "echo", {"value": "a"})  # 在模型步骤之后、工具步骤之前
    assert dispatched == [("echo", {"value": "a"})] and isinstance(out[0], AIMessage)


def test_live_sink_is_only_used_by_an_executor():
    assert agent._live_sink(SimpleNamespace(executor=None, run=SimpleNamespace(id="r1"))) is None
    toolbox = SimpleNamespace(executor="token", run=SimpleNamespace(id="r1"))
    sink = agent._live_sink(toolbox)
    assert isinstance(sink, live.LiveSink) and agent._live_sink(toolbox) is sink


class FakeRedis:
    def __init__(self, fail=False):
        self.data: dict[str, str] = {}
        self.fail = fail
        self.writes = 0

    def set(self, key, value, ex=None):
        if self.fail:
            raise ConnectionError("down")
        self.writes += 1
        self.data[key] = value

    def get(self, key):
        return self.data.get(key)

    def delete(self, key):
        self.data.pop(key, None)


def test_live_sink_throttles_and_can_be_read_back(monkeypatch):
    monkeypatch.setattr(live, "_unavailable_until", 0.0)  # 不受其他用例触发的暂停写入影响
    redis = FakeRedis()
    monkeypatch.setattr(live, "_client", lambda: redis)
    sink = live.LiveSink("r1")
    sink.model_started(3)
    sink.model_delta("想", "")  # 节流间隔内不写
    assert json.loads(redis.data["avelis:analysis:live:r1"])["thinking"] == ""
    sink.model_delta("想一想", "", force=True)
    state = live.read("r1")
    assert state["phase"] == "model" and state["after_seq"] == 3 and state["thinking"] == "想一想"
    sink.tool_started(4, "query_metric", {"metric_code": "gmv"})
    assert live.read("r1") | {"started_at": None} == {
        "phase": "tool",
        "after_seq": 4,
        "started_at": None,
        "tool": "query_metric",
        "args": {"metric_code": "gmv"},
    }
    live.clear("r1")
    assert live.read("r1") is None


def test_live_sink_gives_up_quietly_when_redis_is_down(monkeypatch):
    monkeypatch.setattr(live, "_unavailable_until", 0.0)
    redis = FakeRedis(fail=True)
    sink = live.LiveSink("r1", client=redis)
    sink.model_started(0)  # 不抛出
    assert sink.disabled
    other = live.LiveSink("r2", client=FakeRedis())
    other.model_started(0)  # 本进程暂停写入一段时间，不再逐次等待连接超时
    assert other._client.writes == 0
    monkeypatch.setattr(live, "_client", lambda: (_ for _ in ()).throw(ConnectionError("down")))
    assert live.read("r1") is None


@pytest.mark.parametrize(
    ("status", "stored", "expected"),
    [
        (
            "running",
            {"phase": "model", "after_seq": 2, "started_at": "2026-11-19T08:00:00+00:00", "thinking": "想"},
            "model",
        ),
        ("running", {"phase": "model", "after_seq": 2, "started_at": "t"}, None),
        ("running", {"phase": "bogus"}, None),
        ("succeeded", {"phase": "model", "after_seq": 2, "started_at": "t"}, None),
        ("running", None, None),
    ],
)
def test_live_endpoint_only_returns_valid_state_of_a_running_run(monkeypatch, status, stored, expected):
    run = SimpleNamespace(id="r1", status=status)
    monkeypatch.setattr(analysis_api.analysis, "visible_run", lambda session, user, run_id: run)
    monkeypatch.setattr(analysis_api.analysis_live, "read", lambda run_id: stored)
    out = analysis_api.get_run_live("r1", user=SimpleNamespace(), session=SimpleNamespace())
    assert (out or {}).get("phase") == expected
