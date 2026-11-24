"""M3B 多模型生态、流式思考提取、主备容灾与复杂度路由测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import openai
import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI

from avelis import modelcfg
from avelis.analysis import agent as analysis_agent
from avelis.analysis import service as analysis_service
from avelis.analysis.tools import Budget
from avelis.llm_protocol import (
    OpenAICompatibleChatOpenAI,
    call_with_failover,
    extract_thinking,
    is_failover_error,
    model_usage,
    sanitize_messages_for_target,
    stream_reply,
    streams_live,
)
from avelis.models import AppSetting, ModelProvider, User
from tests.unit.test_analysis_toolbox import make_box
from tests.unit.test_modelcfg_safety import MemorySession


def _provider(
    pid: str,
    code: str,
    preset: str,
    *,
    kind: str = "openai_compatible",
    base_url: str,
    models: list[str],
    tool_calling: bool = True,
) -> ModelProvider:
    return ModelProvider(
        id=pid,
        code=code,
        name=code,
        kind=kind,
        preset=preset,
        base_url=base_url,
        api_key_enc="encrypted",
        enabled=True,
        models=[{"id": m, "enabled": True, "tool_calling": tool_calling} for m in models],
        last_check={
            "models": {
                m: {"model_id": m, "ok": True, "tool_calling": tool_calling, "checked_at": "2026-11-23T00:00:00Z"}
                for m in models
            }
        },
    )


def test_multimodel_profiles_effort_context_and_builders(monkeypatch):
    monkeypatch.setattr(modelcfg, "decrypt_secret", lambda _: "sk-test-key")

    oai = _provider(
        "p-oai", "openai", "openai", base_url="https://api.openai.com/v1", models=["gpt-4o", "o3", "gpt-5", "gpt-4.1"]
    )
    ant = _provider(
        "p-ant",
        "anthropic",
        "anthropic",
        kind="anthropic",
        base_url="https://api.anthropic.com",
        models=["claude-sonnet-4-5", "claude-3-5-sonnet"],
    )
    qwen = _provider(
        "p-qwen",
        "dashscope",
        "dashscope",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        models=["qwen3-235b", "qwen-long"],
    )
    custom = _provider("p-custom", "custom", "custom", base_url="https://local.invalid/v1", models=["my-llm"])

    # OpenAI reasoning vs non-reasoning
    assert modelcfg.effort_map(oai, "gpt-4o") is None
    assert set(modelcfg.effort_map(oai, "o3")) == {"low", "medium", "high"}
    m_o3 = modelcfg.build_chat_model(oai, "o3", temperature=0.2, effort="high")
    assert isinstance(m_o3, ChatOpenAI) and m_o3.reasoning_effort == "high" and m_o3.temperature is None
    m_4o = modelcfg.build_chat_model(oai, "gpt-4o", temperature=0.2, effort="high")
    assert m_4o.reasoning_effort is None and m_4o.temperature == 0.2
    assert modelcfg._context_window(oai, {"id": "gpt-4.1"}) == (1_047_576, "default")
    assert modelcfg._context_window(oai, {"id": "gpt-5"}) == (400_000, "default")
    assert modelcfg._context_window(oai, {"id": "o3"}) == (200_000, "default")

    # Anthropic thinking vs older Claude
    assert set(modelcfg.effort_map(ant, "claude-sonnet-4-5")) == {"off", "low", "medium", "high", "max"}
    assert modelcfg.effort_map(ant, "claude-3-5-sonnet") is None
    m_claude = modelcfg.build_chat_model(ant, "claude-sonnet-4-5", temperature=0.2, effort="high", max_tokens=2048)
    assert isinstance(m_claude, ChatAnthropic)
    assert m_claude.thinking == {"type": "enabled", "budget_tokens": 10240}
    assert m_claude.temperature is None
    assert m_claude.max_tokens > 10240
    m_claude_off = modelcfg.build_chat_model(ant, "claude-sonnet-4-5", temperature=0.2, effort="off", max_tokens=1000)
    assert m_claude_off.thinking is None and m_claude_off.temperature == 0.2 and m_claude_off.max_tokens == 1000
    assert modelcfg._context_window(ant, {"id": "claude-sonnet-4-5"}) == (200_000, "default")

    # DashScope
    m_qwen = modelcfg.build_chat_model(qwen, "qwen3-235b", effort="low", max_tokens=1500)
    assert isinstance(m_qwen, OpenAICompatibleChatOpenAI)
    assert m_qwen.extra_body == {"enable_thinking": True, "thinking_budget": 1024}
    assert modelcfg._context_window(qwen, {"id": "qwen-long"}) == (1_000_000, "default")

    # Custom remains unguessed
    assert modelcfg.effort_map(custom, "my-llm") is None
    assert modelcfg._context_window(custom, {"id": "my-llm"}) == (None, None)

    # streams_live broadened to ChatModels, False for stubs
    assert streams_live(m_o3) and streams_live(m_claude) and streams_live(m_qwen)
    assert not streams_live(SimpleNamespace(invoke=lambda *_: None))


def test_extract_thinking_and_anthropic_openai_sse_streaming():
    # extract_thinking across OpenAI-compatible & Anthropic blocks
    ds_msg = AIMessage(content="hello", additional_kwargs={"reasoning_content": "step1"})
    ant_msg = AIMessage(content=[{"type": "thinking", "thinking": "plan"}, {"type": "text", "text": "answer"}])
    assert extract_thinking(ds_msg) == "step1"
    assert extract_thinking(ant_msg) == "plan"

    # Cross-provider sanitization
    sanitized_for_oai = sanitize_messages_for_target([ds_msg, ant_msg], ChatOpenAI(model="gpt-4o", api_key="k"))
    assert "reasoning_content" not in sanitized_for_oai[0].additional_kwargs
    assert sanitized_for_oai[1].content == "answer"

    # Anthropic SSE stream_reply via httpx2.MockTransport
    import anthropic
    import httpx2

    ant_events = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "m1",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-4-5",
                    "content": [],
                    "stop_reason": None,
                    "usage": {"input_tokens": 50, "output_tokens": 0},
                },
            },
        ),
        (
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        ),
        (
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "先分析"}},
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "content_block_start",
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        ),
        (
            "content_block_delta",
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "结论如下"}},
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 1}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"input_tokens": 50, "output_tokens": 20},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    ant_sse = "".join(f"event: {ev}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n" for ev, data in ant_events)
    ant_transport = httpx2.MockTransport(
        lambda req: httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=ant_sse.encode("utf-8"))
    )

    ant_model = ChatAnthropic(
        model="claude-sonnet-4-5",
        api_key="sk-ant-test",
        base_url="https://api.anthropic.com",
    )
    ant_model._client = anthropic.Anthropic(
        api_key="sk-ant-test",
        base_url="https://api.anthropic.com",
        http_client=httpx2.Client(transport=ant_transport),
    )
    deltas: list[tuple[str, str]] = []
    reply = stream_reply(ant_model, [HumanMessage("hi")], lambda t, x: deltas.append((t, x)), interval=0)
    assert extract_thinking(reply) == "先分析"
    assert ("先分析", "") in deltas and ("先分析", "结论如下") in deltas
    assert model_usage(reply)["input_tokens"] == 50 and model_usage(reply)["output_tokens"] == 20

    # DashScope OpenAICompatibleChatOpenAI SSE stream_reply
    ds_chunks = [
        {
            "id": "1",
            "choices": [
                {"index": 0, "delta": {"role": "assistant", "reasoning_content": "思考中"}, "finish_reason": None}
            ],
        },
        {
            "id": "1",
            "choices": [{"index": 0, "delta": {"content": "结果"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40},
        },
    ]
    ds_sse = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in ds_chunks) + "data: [DONE]\n\n"
    qwen_transport = httpx.MockTransport(
        lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=ds_sse.encode("utf-8"))
    )
    qwen_model = OpenAICompatibleChatOpenAI(
        model="qwen3-235b",
        api_key="sk-qwen-test",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        http_client=httpx.Client(transport=qwen_transport),
    )
    q_deltas: list[tuple[str, str]] = []
    q_reply = stream_reply(qwen_model, [HumanMessage("hi")], lambda t, x: q_deltas.append((t, x)), interval=0)
    assert extract_thinking(q_reply) == "思考中"
    assert q_reply.content == "结果"
    assert model_usage(q_reply)["input_tokens"] == 30 and model_usage(q_reply)["output_tokens"] == 10


def test_backups_routing_and_policy_isolation(monkeypatch):
    monkeypatch.setattr(modelcfg, "decrypt_secret", lambda _: "sk-test")
    monkeypatch.setattr(modelcfg, "build_chat_model", lambda p, m, **kw: SimpleNamespace(provider=p.id, model_id=m))
    monkeypatch.setattr(modelcfg.audit, "record", lambda *a, **kw: None)

    oai = _provider("p-oai", "openai", "openai", base_url="https://api.openai.com/v1", models=["gpt-4o", "gpt-5"])
    ant = _provider(
        "p-ant",
        "anthropic",
        "anthropic",
        kind="anthropic",
        base_url="https://api.anthropic.com",
        models=["claude-sonnet-4-5"],
    )
    session = MemorySession(oai)
    session.rows[ModelProvider, ant.id] = ant
    actor = User(id="admin", username="admin")

    view = modelcfg.save_settings(
        session,
        actor,
        {
            "ask": {
                "provider_id": oai.id,
                "model_id": "gpt-5",
                "reasoning_effort": "medium",
                "backups": [
                    {"provider_id": ant.id, "model_id": "claude-sonnet-4-5", "reasoning_effort": "low"},
                    {"provider_id": oai.id, "model_id": "gpt-5"},  # deduplicated against primary
                ],
            },
            "analysis": {
                "provider_id": ant.id,
                "model_id": "claude-sonnet-4-5",
                "backups": [{"provider_id": oai.id, "model_id": "gpt-4o"}],
            },
        },
        {"send_results": False, "send_code": False},
        new_routing={
            "enabled": True,
            "simple_target": {"provider_id": oai.id, "model_id": "gpt-4o"},
            "complex_target": {"provider_id": oai.id, "model_id": "gpt-5", "reasoning_effort": "high"},
        },
    )

    # model_policy remains strictly {send_results, send_code}
    assert modelcfg.policy(session) == {"send_results": False, "send_code": False}
    assert len(view["assignments"]["ask"]["backups"]) == 1
    assert view["routing"]["enabled"] is True

    # Max 5 backups enforced
    too_many = [{"provider_id": ant.id, "model_id": "claude-sonnet-4-5"} for _ in range(6)]
    with pytest.raises(modelcfg.Invalid):
        modelcfg.save_settings(
            session, actor, {"ask": {"provider_id": oai.id, "model_id": "gpt-4o", "backups": too_many}}, None
        )

    # Complexity classification & routing
    assert modelcfg.classify_ask_complexity("昨日指标是多少") == "simple"
    assert modelcfg.classify_ask_complexity("请对比华东和华南近30天的趋势占比，以及为什么下滑") == "complex"
    assert modelcfg.classify_analysis_complexity("查一下昨天订单数") == "simple"
    assert modelcfg.classify_analysis_complexity("为什么本周指标突然波动下降", has_skills=False) == "complex"

    simple_res = modelcfg.resolve_routed(session, "ask", complexity="simple")
    assert simple_res.adapter == "openai:gpt-4o" and simple_res.routing_tier == "simple"
    complex_res = modelcfg.resolve_routed(session, "ask", complexity="complex")
    assert complex_res.adapter == "openai:gpt-5" and complex_res.routing_tier == "complex"

    # Explicit model_choice in Analysis ALWAYS bypasses routing
    session.rows[AppSetting, modelcfg.POLICY_KEY].value = {"send_results": True, "send_code": True}
    explicit = analysis_service.require_analysis_model(
        session,
        {"provider_id": ant.id, "model_id": "claude-sonnet-4-5", "reasoning_effort": "low"},
        question="昨日指标是多少",
    )
    assert explicit.adapter == "anthropic:claude-sonnet-4-5" and explicit.routing_tier is None


def test_failover_in_call_with_failover_and_analysis_invoke(monkeypatch):
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    rate_err = openai.RateLimitError("429", response=httpx.Response(429, request=req), body={})
    bad_req = openai.BadRequestError("400", response=httpx.Response(400, request=req), body={})
    assert is_failover_error(rate_err) and not is_failover_error(bad_req)

    # Backup succeeds after primary 429; stream reset callback called
    fb = SimpleNamespace(
        adapter="anthropic:claude-sonnet-4-5",
        model=object(),
        fallbacks=(),
        provider_id="p2",
        model_id="claude-sonnet-4-5",
        provider_code="anthropic",
        routing_tier=None,
    )
    primary = SimpleNamespace(
        adapter="openai:gpt-5",
        model=object(),
        fallbacks=(fb,),
        provider_id="p1",
        model_id="gpt-5",
        provider_code="openai",
        routing_tier="complex",
    )
    resets: list[bool] = []

    def _fn(cand):
        if cand is primary:
            raise rate_err
        return AIMessage("ok-from-backup")

    out, used, failover_from = call_with_failover(primary, _fn, reset_stream=lambda: resets.append(True))
    assert out.content == "ok-from-backup" and used is fb and failover_from == "openai:gpt-5" and resets == [True]

    # Analysis _invoke falls over from primary AuthError to backup model
    box = make_box(budget=Budget(model_requests=10), monkeypatch=monkeypatch)
    box.adapter = "openai:gpt-5"
    auth_err = openai.AuthenticationError("bad key", response=httpx.Response(401, request=req), body={})

    class FakeModel:
        def __init__(self, reply_or_exc):
            self.reply_or_exc = reply_or_exc

        def bind_tools(self, *a, **kw):
            return self

        def invoke(self, msgs):
            if isinstance(self.reply_or_exc, Exception):
                raise self.reply_or_exc
            return self.reply_or_exc

    fb_resolved = SimpleNamespace(
        adapter="anthropic:claude-sonnet-4-5",
        model=FakeModel(AIMessage("backup-analysis-ok")),
        factory=lambda **kw: FakeModel(AIMessage("backup-analysis-ok")),
        context_window=200_000,
        provider_id="p2",
        provider_code="anthropic",
        model_id="claude-sonnet-4-5",
        routing_tier=None,
        fallbacks=(),
    )
    pri_resolved = SimpleNamespace(
        adapter="openai:gpt-5",
        model=FakeModel(auth_err),
        factory=lambda **kw: FakeModel(auth_err),
        context_window=400_000,
        provider_id="p1",
        provider_code="openai",
        model_id="gpt-5",
        routing_tier="complex",
        fallbacks=(fb_resolved,),
    )
    reply = analysis_agent._round({"messages": [HumanMessage("分析")]}, box, pri_resolved)
    assert reply and reply[0].content == "backup-analysis-ok"
    assert box.adapter == "anthropic:claude-sonnet-4-5"
    ok_step = [s for s in box.session.added if s.kind == "model" and s.status == "ok"][0]
    assert ok_step.output["failover_from"] == "openai:gpt-5"
    assert ok_step.output["adapter"] == "anthropic:claude-sonnet-4-5"
