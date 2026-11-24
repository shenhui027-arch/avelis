"""可选 OpenTelemetry 分布式追踪集成钩子：默认关闭且零强依赖。"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def init_tracing(app, settings) -> bool:
    """可选 OpenTelemetry 接入钩子：默认关闭，未装可选包时记录告警并降级为无操作。"""
    if not getattr(settings, "otel_enabled", False):
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
    except ImportError:
        logger.warning("AVELIS_OTEL_ENABLED=true 但未安装 opentelemetry 可选依赖，已跳过追踪初始化")
        return False

    provider = TracerProvider(resource=Resource.create({"service.name": "avelis"}))
    endpoint = (getattr(settings, "otel_endpoint", "") or "").strip()
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        except ImportError:
            logger.warning("已配置 OTLP 端点但缺少 opentelemetry-exporter-otlp，将仅启用本地 TracerProvider")
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)
    return True
