(
    """模型提供方协议适配（任务书 P0-E）：按提供方官方协议处理 LangChain 通用实现覆盖不到的字段与终止原因。

- DeepSeek 思考模式的 reasoning_content（https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/）：官方要求
  “携带了 tools 参数的请求，在后续所有请求中，必须完整回传 reasoning_content 给 API”，否则返回 400。
  langchain_openai.ChatOpenAI 只按 OpenAI 规范解析，既不读取也不回传这个字段，这里补上受控的原样往返：
  响应里的 reasoning_content 放进 AIMessage.additional_kwargs（不进入 content，不展示给用户，不写步骤记录与普通日志），
  之后的请求把同一条助手消息的 reasoning_content 原样带回。消息对象可以按 LangChain 的序列化保存与恢复，字段随之保留。
- DeepSeek 官方 Chat Completions 的输出上限字段是 max_tokens；langchain_openai 1.7.1 改发的 max_completion_tokens
  经实测被忽略（上限 8，实际输出 141 token），因此请求时改回 max_tokens。
- finish_reason=length / stop_reason=max_tokens（输出被截断）与 content_filter 不是模型的正常回答，"""
    """按基础设施错误处理（ModelProtocolError），
  不把截断的工具参数当作模型写错的内容去修复或重交。
- 工具调用编号：同一条回复里的工具调用必须各有编号且互不重复，否则无法逐个回复（下一次请求会被拒），也按协议错误处理。
"""
)

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

REASONING_FIELD = "reasoning_content"
ABNORMAL_FINISH = {
    "length": "model_truncated",
    "max_tokens": "model_truncated",
    "content_filter": "model_content_filter",
}


class ModelProtocolError(RuntimeError):
    """模型回复不符合接入协议（截断、内容过滤、工具调用编号缺失或重复）：基础设施错误，不是分析内容的错误。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


class ModelTruncated(ModelProtocolError):
    """finish_reason=length 或 stop_reason=max_tokens：输出达到上限被截断。"""


def response_finish_reason(reply: AIMessage) -> str | None:
    """读取提供方的原始终止原因，兼容 OpenAI 与 Anthropic 的字段。"""
    metadata = getattr(reply, "response_metadata", None) or {}
    return metadata.get("finish_reason") or metadata.get("stop_reason")


def protocol_problem(reply: AIMessage) -> ModelProtocolError | None:
    """一次回复是否违反接入协议；正常时返回 None。没有 content 也没有工具调用的回复是合法的（由调用方提示继续）。"""
    finish = response_finish_reason(reply)
    if finish in ABNORMAL_FINISH:
        cls = ModelTruncated if finish in ("length", "max_tokens") else ModelProtocolError
        return cls(ABNORMAL_FINISH[finish], f"模型回复的 finish_reason 为 {finish}（输出不完整，不作为模型内容处理）")
    ids = [c.get("id") for c in [*(reply.tool_calls or []), *(getattr(reply, "invalid_tool_calls", None) or [])]]
    if any(not i for i in ids):
        return ModelProtocolError("model_protocol", "工具调用缺少编号，无法逐个回复")
    if len(set(ids)) != len(ids):
        return ModelProtocolError("model_protocol", "同一条回复里的工具调用编号重复")
    return None


class ReasoningRoundTripChatOpenAI(ChatOpenAI):
    """OpenAI 兼容接口 + reasoning_content 原样往返（DeepSeek 思考模式）。只回传提供方自己返回过的字段。"""

    def _create_chat_result(self, response: Any, generation_info: dict | None = None):
        result = super()._create_chat_result(response, generation_info)
        data = response if isinstance(response, dict) else response.model_dump()
        for gen, choice in zip(result.generations, data.get("choices") or []):
            reasoning = (choice.get("message") or {}).get(REASONING_FIELD)
            if reasoning is not None and isinstance(gen.message, AIMessage):
                gen.message.additional_kwargs[REASONING_FIELD] = reasoning
        return result

    def _get_request_payload(self, input_: Any, *, stop: list[str] | None = None, **kwargs: Any) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if "max_completion_tokens" in payload:
            payload["max_tokens"] = payload.pop("max_completion_tokens")
        messages = self._convert_input(input_).to_messages()
        for message, out in zip(messages, payload.get("messages") or []):  # 与父类逐条转换，顺序一一对应
            if isinstance(message, AIMessage) and REASONING_FIELD in message.additional_kwargs:
                out[REASONING_FIELD] = message.additional_kwargs[REASONING_FIELD]
        return payload
