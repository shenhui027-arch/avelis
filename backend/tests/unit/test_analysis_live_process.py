"""运行中的实时过程：流式回复按官方协议合并（推理文字、工具调用、用量、终止原因与非流式一致），
推理文字边收边交给实时缓冲；正在执行的工具写入实时缓冲；Redis 不可用时不影响分析；
运行详情里的实时过程与步骤同一套当前权限投影；流在终止块之前结束按传输层错误处理。"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from avelis.analysis import agent, live, service
from avelis.llm_protocol import ReasoningRoundTripChatOpenAI, response_finish_reason, stream_reply, streams_live
from tests.unit.test_analysis_dataset_access import dataset_db  # noqa: F401 - 共用撤权夹具
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
    ("stored", "expected"),
    [
        ({"phase": "model", "after_seq": 2, "started_at": "2026-11-19T08:00:00+00:00", "thinking": "想"}, "model"),
        ({"phase": "tool", "after_seq": 2, "started_at": "2026-11-19T08:00:00", "tool": "query_metric"}, "tool"),
        ({"phase": "model", "after_seq": 2, "started_at": "t"}, None),
        ({"phase": "model", "after_seq": "2", "started_at": "2026-11-19T08:00:00"}, None),
        ({"phase": "bogus"}, None),
        (None, None),
    ],
)
def test_live_view_drops_incomplete_state(stored, expected):
    out = live.view(stored, restricted=False, project_args=dict)
    assert (out or {}).get("phase") == expected
    if out:
        assert out["elapsed_ms"] >= 0 and out["restricted"] is False


LIVE_TOOL = {
    "phase": "tool",
    "after_seq": 1,
    "started_at": "2026-11-19T08:00:00",
    "tool": "query_sql",
    "args": {"sql": "SELECT 7654321", "metric_code": "gmv", "start_date": "2026-09-01", "note": "受限成员甲"},
}
LIVE_MODEL = {
    "phase": "model",
    "after_seq": 1,
    "started_at": "2026-11-19T08:00:00",
    "thinking": "推理包含受限成员甲",
    "text": "模型复述 7654321",
}


@pytest.mark.parametrize("state", [LIVE_TOOL, LIVE_MODEL])
def test_run_view_applies_current_permissions_to_live_state(dataset_db, monkeypatch, state):
    """数据权限在模型流式回复期间被撤销：下一次读取运行详情时，实时过程按与步骤相同的投影收紧。"""
    session, user, run, _, asset, _, _ = dataset_db
    run.status = "running"
    session.commit()
    monkeypatch.setattr(live, "read", lambda run_id: state)
    before = service.run_view(session, run, user=user)["live"]
    assert before["restricted"] is False
    assert before["args"] == state.get("args", {}) and before["thinking"] == state.get("thinking")

    asset.domain_id = "hidden"
    session.commit()
    after = service.run_view(session, run, user=user)["live"]
    assert after["restricted"] is True and after["thinking"] is None and after["text"] is None
    assert after["args"] == ({"metric_code": "gmv", "start_date": "2026-09-01"} if state is LIVE_TOOL else {})
    assert "7654321" not in json.dumps(after, ensure_ascii=False)
    assert "受限成员甲" not in json.dumps(after, ensure_ascii=False)

    run.status = "succeeded"
    session.commit()
    assert service.run_view(session, run, user=user)["live"] is None
    assert service.run_view(session, run, full=False, user=user).get("live") is None


def test_stream_ending_without_a_terminal_chunk_is_a_transport_error():
    """提供方或代理在终止块之前干净地关闭连接：不当作完整回复，也不执行其中的工具调用。"""

    def handler(request: httpx.Request) -> httpx.Response:
        events = [
            _chunk({"role": "assistant", "content": None, "reasoning_content": "想"}),
            _chunk(
                {
                    "tool_calls": [
                        {"index": 0, "id": "c", "type": "function", "function": {"name": "echo", "arguments": "{}"}}
                    ]
                }
            ),
        ]
        return httpx.Response(200, content=_sse(*events), headers={"content-type": "text/event-stream"})

    model = ReasoningRoundTripChatOpenAI(
        model="deepseek-flash",
        api_key="unit-test-key",
        base_url="https://deepseek.invalid",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=0,
    )
    with pytest.raises(httpx.RemoteProtocolError):
        stream_reply(model, [HumanMessage("x")], lambda t, x: None)
    assert agent._is_transient_error(httpx.RemoteProtocolError("x"))  # 与连接中断一样按临时错误重试一次


def test_live_read_backs_off_after_a_redis_error(monkeypatch):
    monkeypatch.setattr(live, "_unavailable_until", 0.0)
    calls = []

    def down():
        calls.append(1)
        raise ConnectionError("down")

    monkeypatch.setattr(live, "_client", down)
    assert live.read("r1") is None and live.read("r1") is None
    assert len(calls) == 1  # 第二次不再等待连接超时
