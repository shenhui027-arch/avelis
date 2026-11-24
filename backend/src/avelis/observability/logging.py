"""生产日志规范：统一脱敏过滤、结构化 JSON / 文本格式器与 Uvicorn 日志集成。"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from avelis.observability.context import get_log_context

_STANDARD_LOG_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()) | {
    "message",
    "asctime",
    "trace_ctx",
}
_CORE_JSON_KEYS = frozenset(
    {
        "timestamp",
        "level",
        "service",
        "env",
        "logger",
        "message",
        "trace_id",
        "span_id",
        "parent_span_id",
        "request_id",
        "user_id",
        "job_id",
        "client_ip",
        "error_type",
        "error_message",
        "exception",
    }
)

_BEARER_RE = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{4,}", re.IGNORECASE)
_TOKEN_PREFIX_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|(?:pat|avp)_[A-Za-z0-9_-]{12,})")
_DSN_CRED_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+:)([^@\s/]+)(@)")
_KV_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|secret|api_key|apikey|access_token|refresh_token|authorization)\s*([:=])\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)


def redact_secrets(text: str) -> str:
    """对日志文本中的 Bearer/API Key/PAT/DSN 口令与敏感键值做统一脱敏。"""
    if not text:
        return text
    out = _DSN_CRED_RE.sub(r"\g<1>***\g<3>", text)
    out = _BEARER_RE.sub(lambda m: f"{m.group(1)} [REDACTED]", out)
    out = _TOKEN_PREFIX_RE.sub("[REDACTED]", out)
    out = _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", out)
    return out


class SecretRedactingFilter(logging.Filter):
    """日志过滤器：确保敏感凭据不会以原文形式写入日志流。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - 非标准 args 兜底
            msg = str(record.msg)
        redacted = redact_secrets(msg)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


def _sanitize_extra_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _sanitize_extra_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_extra_value(v) for v in value]
    return redact_secrets(str(value))


class JsonLogFormatter(logging.Formatter):
    """生产级单行 UTF-8 JSON 日志格式器，包含 service/env/trace_id/span_id 与脱敏保护。"""

    def __init__(self, service: str = "avelis-api", env: str = "production") -> None:
        super().__init__()
        self.service = service
        self.env = env

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds")
        payload: dict[str, Any] = {
            "timestamp": ts.replace("+00:00", "Z"),
            "level": record.levelname,
            "service": self.service,
            "env": self.env,
            "logger": record.name,
            "message": redact_secrets(record.getMessage()),
        }
        payload.update(get_log_context())
        for key, val in record.__dict__.items():
            if key not in _STANDARD_LOG_ATTRS and key not in _CORE_JSON_KEYS and not key.startswith("_"):
                payload[key] = _sanitize_extra_value(val)
        if record.exc_info:
            exc_type, exc_val, _ = record.exc_info
            if exc_type is not None:
                payload["error_type"] = exc_type.__name__
            if exc_val is not None:
                payload["error_message"] = redact_secrets(str(exc_val))
            payload["exception"] = redact_secrets(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextLogFormatter(logging.Formatter):
    """研发与排障友好文本格式器，在请求或任务上下文内自动附加紧凑 trace/request 标识。"""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s [%(name)s]%(trace_ctx)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        ctx = get_log_context()
        tags: list[str] = []
        if tid := ctx.get("trace_id"):
            tags.append(f"trace={tid[:12]}")
        if rid := ctx.get("request_id"):
            tags.append(f"req={rid[:12]}")
        if uid := ctx.get("user_id"):
            tags.append(f"uid={uid[:8]}")
        if jid := ctx.get("job_id"):
            tags.append(f"job={jid[:8]}")
        record.trace_ctx = f" [{' '.join(tags)}]" if tags else ""
        return redact_secrets(super().format(record))


def configure_logging(
    level: str = "info",
    fmt: str = "text",
    *,
    service: str = "avelis-api",
    env: str = "production",
) -> None:
    """按配置设置日志级别、格式器（text/json）与脱敏过滤器，同时保留 pytest caplog 捕获器。"""
    normalized_level = (level or "info").strip().lower()
    numeric = logging.DEBUG if normalized_level == "trace" else getattr(logging, normalized_level.upper(), logging.INFO)
    handler = logging.StreamHandler()
    handler.addFilter(SecretRedactingFilter())
    if (fmt or "text").strip().lower() == "json":
        handler.setFormatter(JsonLogFormatter(service=service, env=env))
    else:
        handler.setFormatter(TextLogFormatter())
    root = logging.getLogger()
    preserved = [h for h in root.handlers if type(h).__module__.startswith("_pytest")]
    logging.basicConfig(level=numeric, handlers=[*preserved, handler], force=True)


def build_uvicorn_log_config(settings) -> dict[str, Any]:
    """构造传给 uvicorn 的日志字典配置，确保 uvicorn 自身日志也遵守 json/text 规范并关闭重复 access log。"""
    normalized = (getattr(settings, "log_level", "info") or "info").strip().lower()
    uvicorn_level = "DEBUG" if normalized == "trace" else normalized.upper()
    fmt_name = (getattr(settings, "log_format", "text") or "text").strip().lower()
    env_name = getattr(settings, "environment", "production")
    formatter_cfg: dict[str, Any] = (
        {"()": "avelis.observability.logging.JsonLogFormatter", "service": "avelis-api", "env": env_name}
        if fmt_name == "json"
        else {"()": "avelis.observability.logging.TextLogFormatter"}
    )
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"redact": {"()": "avelis.observability.logging.SecretRedactingFilter"}},
        "formatters": {"avelis": formatter_cfg},
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "formatter": "avelis",
                "filters": ["redact"],
                "stream": "ext://sys.stderr",
            }
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": uvicorn_level, "propagate": False},
            "uvicorn.error": {"level": uvicorn_level},
            "uvicorn.access": {"handlers": [], "level": "WARNING", "propagate": False},
        },
    }
