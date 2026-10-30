"""回放用的假模型（ADR 0009 第 5 点，最小版本）：按顺序返回预先写好的消息。

轨迹文件（traces/*.json）按字段白名单保存：顶层只有 version、name、question、window、messages；每条消息只有
content 与 tool_calls（每个调用只有 name、args）。不保存工具结果、密钥、连接信息与模型的其他元数据。
以后用真实模型录制时，同样只把白名单字段写进轨迹文件。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage

TRACES = Path(__file__).with_name("traces")
TRACE_FIELDS = {"version", "name", "question", "window", "messages"}
MESSAGE_FIELDS = {"content", "tool_calls"}
CALL_FIELDS = {"name", "args"}


def load_trace(name: str) -> dict[str, Any]:
    trace = json.loads((TRACES / f"{name}.json").read_text(encoding="utf-8"))
    extra = set(trace) - TRACE_FIELDS
    assert not extra, f"轨迹 {name} 含白名单以外的字段：{extra}"
    for i, message in enumerate(trace["messages"]):
        assert not set(message) - MESSAGE_FIELDS, f"轨迹 {name} 第 {i + 1} 条消息含白名单以外的字段"
        for call in message.get("tool_calls") or []:
            assert not set(call) - CALL_FIELDS, f"轨迹 {name} 第 {i + 1} 条消息的工具调用含白名单以外的字段"
    return trace


class ScriptedModel:
    """按顺序返回轨迹里的消息；轨迹用完还被请求时直接失败（回放必须与图流程一一对应）。"""

    def __init__(self, messages: list[dict[str, Any]]):
        self.messages, self.calls, self.seen = list(messages), 0, []

    def bind_tools(self, tools):
        self.tools = [t.name for t in tools]
        return self

    def invoke(self, messages):
        self.seen.append(messages)
        if self.calls >= len(self.messages):
            raise AssertionError(f"回放轨迹只有 {len(self.messages)} 条消息，图流程请求了第 {self.calls + 1} 条")
        message = self.messages[self.calls]
        self.calls += 1
        return AIMessage(content=message.get("content", ""), tool_calls=[
            {"name": c["name"], "args": c["args"], "id": f"replay_{self.calls}_{i}"}
            for i, c in enumerate(message.get("tool_calls") or [])])
