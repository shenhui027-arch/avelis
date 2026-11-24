"""请求与后台任务链路上下文：W3C traceparent / X-Trace-ID / span_id / X-Request-ID / user_id / job_id。"""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar

from avelis.auth_config import is_trusted_proxy_ip
from avelis.config import get_settings

_SAFE_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$", re.IGNORECASE)
_TRACE_HEX_RE = re.compile(r"^[0-9a-f]{32}$")

_trace_id: ContextVar[str | None] = ContextVar("avelis_trace_id", default=None)
_span_id: ContextVar[str | None] = ContextVar("avelis_span_id", default=None)
_parent_span_id: ContextVar[str | None] = ContextVar("avelis_parent_span_id", default=None)
_request_id: ContextVar[str | None] = ContextVar("avelis_request_id", default=None)
_client_ip: ContextVar[str | None] = ContextVar("avelis_client_ip", default=None)
_user_agent: ContextVar[str | None] = ContextVar("avelis_user_agent", default=None)
_user_id: ContextVar[str | None] = ContextVar("avelis_user_id", default=None)
_job_id: ContextVar[str | None] = ContextVar("avelis_job_id", default=None)


def parse_traceparent(header: str) -> tuple[str, str] | None:
    """解析 W3C traceparent（00-<32hex trace_id>-<16hex parent_id>-<2hex flags>）；全零 ID 视为无效。"""
    match = _TRACEPARENT_RE.fullmatch((header or "").strip())
    if not match:
        return None
    trace_id, parent_span_id = match.group(1).lower(), match.group(2).lower()
    if trace_id == "0" * 32 or parent_span_id == "0" * 16:
        return None
    return trace_id, parent_span_id


def set_request_context(
    *,
    trace_id: str | None = None,
    span_id: str | None = None,
    parent_span_id: str | None = None,
    request_id: str | None = None,
    client_ip: str | None = None,
    user_agent: str | None = None,
    user_id: str | None = None,
    job_id: str | None = None,
) -> tuple[object, ...]:
    return (
        _trace_id.set(trace_id),
        _span_id.set(span_id),
        _parent_span_id.set(parent_span_id),
        _request_id.set(request_id),
        _client_ip.set(client_ip),
        _user_agent.set(user_agent),
        _user_id.set(user_id),
        _job_id.set(job_id),
    )


def reset_request_context(tokens: tuple[object, ...]) -> None:
    vars_list = (
        _trace_id,
        _span_id,
        _parent_span_id,
        _request_id,
        _client_ip,
        _user_agent,
        _user_id,
        _job_id,
    )
    for var, tok in zip(vars_list, tokens, strict=True):
        var.reset(tok)


def bind_request_context(*, user_id: str | None = None, job_id: str | None = None) -> None:
    """在请求或任务执行过程中补充已认证 user_id 或 job_id，供后续日志与审计自动关联。"""
    if user_id is not None:
        _user_id.set(user_id)
    if job_id is not None:
        _job_id.set(job_id)


def get_request_context() -> dict[str, str]:
    """返回需要封入审计事件明细（row_hash 保护）的链路溯源字段。"""
    out: dict[str, str] = {}
    if tid := _trace_id.get():
        out["trace_id"] = tid
    if rid := _request_id.get():
        out["request_id"] = rid
    if ip := _client_ip.get():
        out["client_ip"] = ip
    if ua := _user_agent.get():
        out["user_agent"] = ua
    if jid := _job_id.get():
        out["job_id"] = jid
    return out


def get_log_context() -> dict[str, str]:
    """返回当前活跃的完整日志链路关联字段（trace/span/request/user/job/client_ip）。"""
    pairs = (
        ("trace_id", _trace_id.get()),
        ("span_id", _span_id.get()),
        ("parent_span_id", _parent_span_id.get()),
        ("request_id", _request_id.get()),
        ("user_id", _user_id.get()),
        ("job_id", _job_id.get()),
        ("client_ip", _client_ip.get()),
    )
    return {k: v for k, v in pairs if v}


def extract_client_ip(headers: dict[str, str], peer_host: str | None, trusted_proxies: str) -> str | None:
    """仅当直连来源处于受信任代理列表内时采信 X-Forwarded-For / X-Real-IP。"""
    trusted = False
    if trusted_proxies:
        if trusted_proxies.strip() == "*":
            trusted = bool(peer_host)
        else:
            trusted = is_trusted_proxy_ip(peer_host, trusted_proxies)
    if trusted:
        xff = headers.get("x-forwarded-for", "")
        if xff:
            first = xff.split(",")[0].strip()
            if first:
                return first[:64]
        real_ip = headers.get("x-real-ip", "").strip()
        if real_ip:
            return real_ip[:64]
    return peer_host[:64] if peer_host else None


class RequestContextMiddleware:
    """纯 ASGI 中间件：解析并回传 W3C traceparent / X-Trace-ID / X-Request-ID，并采集可信 client_ip。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in (scope.get("headers") or ())}
        tp = parse_traceparent(headers.get("traceparent", ""))
        parent_span_id: str | None = None
        if tp is not None:
            trace_id, parent_span_id = tp
        else:
            raw_tid = (headers.get("x-trace-id") or "").strip()
            trace_id = raw_tid if _SAFE_REQUEST_ID_RE.fullmatch(raw_tid) else uuid.uuid4().hex
        span_id = uuid.uuid4().hex[:16]

        raw_rid = (headers.get("x-request-id") or "").strip()
        req_id = raw_rid if _SAFE_REQUEST_ID_RE.fullmatch(raw_rid) else uuid.uuid4().hex
        peer = scope.get("client")[0] if scope.get("client") else None
        try:
            trusted_proxies = get_settings().forwarded_allow_ips
        except Exception:  # noqa: BLE001 - 离线测试或未初始化配置回退直连 IP
            trusted_proxies = ""
        client_ip = extract_client_ip(headers, peer, trusted_proxies)
        ua = (headers.get("user-agent") or "").strip()[:256] or None

        tokens = set_request_context(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            request_id=req_id,
            client_ip=client_ip,
            user_agent=ua,
        )

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                resp_headers = list(message.get("headers", ()))
                existing = {k.lower() for k, _ in resp_headers}
                if b"x-request-id" not in existing:
                    resp_headers.append((b"x-request-id", req_id.encode("latin-1")))
                if b"x-trace-id" not in existing:
                    resp_headers.append((b"x-trace-id", trace_id.encode("latin-1")))
                if b"traceparent" not in existing and _TRACE_HEX_RE.fullmatch(trace_id):
                    resp_headers.append((b"traceparent", f"00-{trace_id}-{span_id}-01".encode("latin-1")))
                message = {**message, "headers": resp_headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers if scope["type"] == "http" else send)
        finally:
            reset_request_context(tokens)
