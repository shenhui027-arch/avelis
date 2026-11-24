"""模型提供方协议适配（任务书 P0-E / M3B）：按提供方官方协议处理字段、流式思考与容灾降级。

- DeepSeek / OpenAI 兼容思考模式的 reasoning_content：
  OpenAICompatibleChatOpenAI 保留 max_tokens 并捕获响应/流式块中的 reasoning_content；
  ReasoningRoundTripChatOpenAI 在此基础上原样回传 assistant 消息的 reasoning_content（DeepSeek 官方要求）。
- Anthropic：支持 content 中的 {"type": "thinking", "thinking": ...} 思考块与流式增量提取。
- finish_reason=length / stop_reason=max_tokens（输出被截断）与 content_filter 按 ModelProtocolError 处理。
- 流式（stream_reply）：对所有支持流式的 BaseChatModel 生效，边收边把累计推理文字与正文交给回调。
- 容灾降级（call_with_failover / is_failover_error）：主模型遭遇限流、超时、连接或服务不可用错误时自动切换备选模型。
"""

from __future__ import annotations

import contextvars
import queue
import ssl
import threading
import time
from typing import Any, Callable

import httpx
import openai
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.messages.utils import message_chunk_to_message
from langchain_openai import ChatOpenAI

REASONING_FIELD = "reasoning_content"
ABNORMAL_FINISH = {
    "length": "model_truncated",
    "max_tokens": "model_truncated",
    "content_filter": "model_content_filter",
}


def has_leaked_tool_markup(text: str) -> bool:
    """识别泄漏到正文的工具调用标记，不解析其中的参数。"""
    text = text.casefold()
    return any(
        marker in text for marker in ("｜dsml｜", "<tool_call>", "</tool_call>", "<function_calls>", "<invoke name=")
    )


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


def extract_thinking(message: Any) -> str:
    """从 AIMessage 或 AIMessageChunk 中提取思考文本，兼容 DeepSeek/DashScope 与 Anthropic thinking 块。"""
    if message is None:
        return ""
    additional = getattr(message, "additional_kwargs", None) or {}
    reasoning = additional.get(REASONING_FIELD)
    if isinstance(reasoning, str) and reasoning:
        return reasoning
    content = getattr(message, "content", None)
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "thinking":
                piece = block.get("thinking")
                if isinstance(piece, str) and piece:
                    parts.append(piece)
        if parts:
            return "".join(parts)
    return ""


def model_usage(reply: AIMessage) -> dict[str, int | None]:
    """一次模型调用的 token 数：输入、输出、前缀缓存命中与推理（thinking）。模型服务没给的记 None。"""
    usage = getattr(reply, "usage_metadata", None) or {}
    raw = (getattr(reply, "response_metadata", None) or {}).get("token_usage") or {}
    cached = raw.get("prompt_cache_hit_tokens")
    if cached is None:
        cached = (usage.get("input_token_details") or {}).get("cache_read")
    reasoning = (raw.get("completion_tokens_details") or {}).get("reasoning_tokens")
    if reasoning is None:
        reasoning = (usage.get("output_token_details") or {}).get("reasoning")
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_hit_tokens": cached,
        "reasoning_tokens": reasoning,
    }


class OpenAICompatibleChatOpenAI(ChatOpenAI):
    """OpenAI 兼容接口（DashScope / Qwen 等）：保留 max_tokens 并捕获响应与流式 delta 中的 reasoning_content。"""

    round_trip_reasoning: bool = False

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
        if self.round_trip_reasoning:
            messages = self._convert_input(input_).to_messages()
            for message, out in zip(messages, payload.get("messages") or []):  # 与父类逐条转换，顺序一一对应
                if isinstance(message, AIMessage) and REASONING_FIELD in message.additional_kwargs:
                    out[REASONING_FIELD] = message.additional_kwargs[REASONING_FIELD]
        return payload

    def _convert_chunk_to_generation_chunk(
        self, chunk: dict, default_chunk_class: type, base_generation_info: dict | None
    ):
        result = super()._convert_chunk_to_generation_chunk(chunk, default_chunk_class, base_generation_info)
        if result is None or not isinstance(result.message, AIMessageChunk):
            return result
        choices = chunk.get("choices") or []
        reasoning = ((choices[0] if choices else {}).get("delta") or {}).get(REASONING_FIELD)
        if isinstance(reasoning, str) and reasoning:
            result.message.additional_kwargs[REASONING_FIELD] = reasoning
        if chunk.get("usage"):
            result.message.response_metadata["token_usage"] = chunk["usage"]
        return result


class ReasoningRoundTripChatOpenAI(OpenAICompatibleChatOpenAI):
    """OpenAI 兼容接口 + reasoning_content 原样往返（DeepSeek 思考模式）。"""

    round_trip_reasoning: bool = True


def streams_live(bound: Any) -> bool:
    """支持流式的 LangChain ChatModel 均走流式；测试替身或显式禁用流式时返回 False。"""
    inner = getattr(bound, "bound", bound)
    return isinstance(inner, BaseChatModel) and not getattr(inner, "disable_streaming", False)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(
        block if isinstance(block, str) else block.get("text", "")
        for block in content or []
        if isinstance(block, str) or isinstance(block, dict) and block.get("type") == "text"
    )


def sanitize_messages_for_target(messages: list[Any], target_model: Any) -> list[Any]:
    """跨提供方容灾切换时清理协议专属思考字段，避免目标模型拒绝异构历史消息。"""
    inner = getattr(target_model, "bound", target_model)
    is_round_trip = isinstance(inner, ReasoningRoundTripChatOpenAI)
    is_anthropic = type(inner).__name__ == "ChatAnthropic"
    sanitized: list[Any] = []
    changed = False
    for msg in messages:
        if not isinstance(msg, AIMessage):
            sanitized.append(msg)
            continue
        new_kwargs = dict(msg.additional_kwargs or {})
        new_content = msg.content
        msg_changed = False
        if not is_round_trip and REASONING_FIELD in new_kwargs:
            new_kwargs.pop(REASONING_FIELD, None)
            msg_changed = True
        if not is_anthropic and isinstance(new_content, list):
            filtered = [
                b
                for b in new_content
                if not (isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking"))
            ]
            if len(filtered) != len(new_content):
                all_text = all(isinstance(b, dict) and b.get("type") == "text" for b in filtered)
                new_content = _text(filtered) if all_text else filtered
                msg_changed = True
        if msg_changed:
            changed = True
            sanitized.append(
                AIMessage(
                    content=new_content,
                    additional_kwargs=new_kwargs,
                    response_metadata=dict(msg.response_metadata or {}),
                    tool_calls=list(msg.tool_calls or []),
                    invalid_tool_calls=list(getattr(msg, "invalid_tool_calls", None) or []),
                    id=msg.id,
                    name=msg.name,
                )
            )
        else:
            sanitized.append(msg)
    return sanitized if changed else messages


def _pump(bound: Any, messages: list, out: queue.Queue, stop: threading.Event) -> None:
    """在单独线程里读流：把每个增量放进队列，结束放 done，出错放 error。stop 置位后在下一个增量处停止并关闭流。"""
    stream = None
    try:
        stream = iter(bound.stream(messages, stream_usage=True))
        for chunk in stream:
            if stop.is_set():
                return
            out.put(("chunk", chunk))
        out.put(("done", None))
    except BaseException as exc:  # noqa: BLE001 - 交回调用线程按原样抛出
        out.put(("error", exc))
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            close()


def stream_reply(
    bound: Any,
    messages: list,
    on_delta: Callable[[str, str], None],
    *,
    deadline: float | None = None,
    interval: float = 0.25,
) -> AIMessage:
    """流式请求一次模型并合并为完整回复；收到增量时把累计的推理文字与正文交给 on_delta（至多每 interval 秒一次）。"""
    chunks: list[AIMessageChunk] = []
    thinking: list[str] = []
    text: list[str] = []
    notified = 0.0
    out: queue.Queue = queue.Queue()
    stop = threading.Event()
    reader = threading.Thread(
        target=contextvars.copy_context().run, args=(_pump, bound, messages, out, stop), daemon=True
    )
    reader.start()
    last_emitted_len = (0, 0)
    try:
        while True:
            wait = None if deadline is None else deadline - time.monotonic()
            if wait is not None and wait <= 0:
                raise TimeoutError("模型流式回复超过剩余时间")
            try:
                kind, value = out.get(timeout=wait)
            except queue.Empty:
                raise TimeoutError("模型流式回复超过剩余时间") from None
            if kind == "done":
                t_full, x_full = "".join(thinking), "".join(text)
                if (len(t_full), len(x_full)) != last_emitted_len:
                    on_delta(t_full, x_full)
                break
            if kind == "error":
                raise value
            if not isinstance(value, AIMessageChunk):
                continue
            chunks.append(value)
            piece = extract_thinking(value)
            if piece:
                thinking.append(piece)
            text_piece = _text(value.content) if value.content else ""
            if text_piece:
                text.append(text_piece)
            now = time.monotonic()
            if (piece or text_piece) and now - notified >= interval:
                notified = now
                t_full, x_full = "".join(thinking), "".join(text)
                last_emitted_len = (len(t_full), len(x_full))
                on_delta(t_full, x_full)
    finally:
        stop.set()
    if not chunks:
        raise ModelProtocolError("model_protocol", "模型流式回复没有任何内容")
    merged = chunks[0] + chunks[1:] if len(chunks) > 1 else chunks[0]
    reply = message_chunk_to_message(merged)
    assert isinstance(reply, AIMessage)
    if response_finish_reason(reply) is None:
        # 连接在终止块之前被干净地关闭（提供方或代理中断）：回复不完整，按传输层错误处理（可重试），不执行其中的工具调用
        raise httpx.RemoteProtocolError("模型流式回复在终止块之前结束")
    return reply


FAILOVER_ERRORS: tuple[type[BaseException], ...] = (
    openai.APIConnectionError,
    openai.InternalServerError,
    openai.RateLimitError,
    openai.AuthenticationError,
    openai.PermissionDeniedError,
    openai.NotFoundError,
    httpx.TimeoutException,
    httpx.TransportError,
    ssl.SSLError,
    TimeoutError,
    ConnectionError,
)
try:
    import anthropic

    FAILOVER_ERRORS += (
        anthropic.RateLimitError,
        anthropic.InternalServerError,
        anthropic.APIConnectionError,
        anthropic.AuthenticationError,
        anthropic.PermissionDeniedError,
        anthropic.NotFoundError,
    )
except ImportError:
    anthropic = None  # type: ignore[assignment]


def is_failover_error(exc: BaseException) -> bool:
    """判断是否属于应自动切换备选模型的上游异常（限流、超时、传输中断、5xx、鉴权/配额或 402/408/429）。"""
    pending: list[tuple[BaseException, int]] = [(exc, 0)]
    seen: set[int] = set()
    while pending:
        current, depth = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, FAILOVER_ERRORS):
            return True
        status_code = getattr(current, "status_code", None)
        if isinstance(status_code, int) and (status_code >= 500 or status_code in (401, 402, 403, 404, 408, 429)):
            return True
        if isinstance(current, ModelProtocolError):
            continue
        if depth < 8:
            for cause in (current.__cause__, current.__context__):
                if cause is not None:
                    pending.append((cause, depth + 1))
    return False


def call_with_failover(
    resolved: Any,
    call_fn: Callable[[Any], Any],
    *,
    reset_stream: Callable[[], None] | None = None,
    on_failover: Callable[[Any, Any, BaseException], None] | None = None,
) -> tuple[Any, Any, str | None]:
    """按 primary -> fallbacks 顺序调用模型；遇到可降级错误时自动切换并记录 failover_from。"""
    raw_fallbacks = getattr(resolved, "fallbacks", None)
    fallbacks = tuple(raw_fallbacks) if isinstance(raw_fallbacks, (list, tuple)) else ()
    candidates = [resolved, *fallbacks]
    last_exc: BaseException | None = None
    for idx, candidate in enumerate(candidates):
        if idx > 0:
            if reset_stream is not None:
                reset_stream()
            if on_failover is not None:
                on_failover(candidates[idx - 1], candidate, last_exc)  # type: ignore[arg-type]
        try:
            result = call_fn(candidate)
            failover_from = candidates[0].adapter if idx > 0 else None
            return result, candidate, failover_from
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if idx + 1 >= len(candidates) or not is_failover_error(exc):
                raise
    raise last_exc  # pragma: no cover
