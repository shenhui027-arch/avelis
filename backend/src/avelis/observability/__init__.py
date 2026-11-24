"""Avelis 可观测性子包：链路追踪上下文、脱敏结构化日志、Prometheus 指标与可选 OTel 集成。"""

from __future__ import annotations

from avelis.observability.context import (
    RequestContextMiddleware,
    bind_request_context,
    extract_client_ip,
    get_log_context,
    get_request_context,
    parse_traceparent,
    reset_request_context,
    set_request_context,
)
from avelis.observability.logging import (
    JsonLogFormatter,
    SecretRedactingFilter,
    TextLogFormatter,
    build_uvicorn_log_config,
    configure_logging,
    redact_secrets,
)
from avelis.observability.metrics import (
    ASK_DURATION_BUCKETS,
    DEFAULT_HTTP_BUCKETS,
    DS_QUERY_DURATION_BUCKETS,
    JOB_DURATION_BUCKETS,
    LLM_DURATION_BUCKETS,
    METRICS,
    MetricsRegistry,
)
from avelis.observability.middleware import MetricsMiddleware
from avelis.observability.tracing import init_tracing

__all__ = [
    "ASK_DURATION_BUCKETS",
    "DEFAULT_HTTP_BUCKETS",
    "DS_QUERY_DURATION_BUCKETS",
    "JOB_DURATION_BUCKETS",
    "LLM_DURATION_BUCKETS",
    "METRICS",
    "JsonLogFormatter",
    "MetricsMiddleware",
    "MetricsRegistry",
    "RequestContextMiddleware",
    "SecretRedactingFilter",
    "TextLogFormatter",
    "bind_request_context",
    "build_uvicorn_log_config",
    "configure_logging",
    "extract_client_ip",
    "get_log_context",
    "get_request_context",
    "init_tracing",
    "parse_traceparent",
    "redact_secrets",
    "reset_request_context",
    "set_request_context",
]
