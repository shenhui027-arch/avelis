"""纯 ASGI HTTP 指标采集与分级访问日志中间件。"""

from __future__ import annotations

import logging
import time
from typing import Any

from avelis.observability.metrics import METRICS

_ACCESS_LOG = logging.getLogger("avelis.access")
_PROBE_ROUTES = frozenset({"/api/health", "/api/health/ready", "/api/health/worker", "/metrics"})


def _strip_root(scope: dict[str, Any]) -> str:
    path = scope.get("path", "")
    root = scope.get("root_path", "")
    if root and (path == root or path.startswith(root + "/")):
        path = path[len(root) :]
    return path


class MetricsMiddleware:
    """纯 ASGI 指标与结构化 HTTP 访问日志中间件；使用路由模板防标签基数爆炸。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status_code = 500
        unhandled = False
        METRICS.http_enter()

        async def send_wrapper(message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException:
            unhandled = True
            raise
        finally:
            METRICS.http_exit()
            elapsed = time.perf_counter() - started
            method = str(scope.get("method", "GET")).upper()
            raw_path = _strip_root(scope)
            route = scope.get("route")
            raw_route_path = getattr(route, "path", None)
            if isinstance(raw_route_path, str) and raw_route_path != "/{path:path}":
                route_path = raw_route_path
            elif raw_path == "/metrics":
                route_path = "/metrics"
            else:
                route_path = "unmatched"
            METRICS.inc_http(method, route_path, status_code, elapsed)
            duration_ms = round(elapsed * 1000, 2)
            extra = {
                "event": "http.access",
                "method": method,
                "path": raw_path,
                "route": route_path,
                "status_code": status_code,
                "duration_ms": duration_ms,
            }
            msg = f"{method} {raw_path} -> {status_code} ({duration_ms}ms)"
            if unhandled or status_code >= 500:
                _ACCESS_LOG.error(msg, extra=extra, exc_info=unhandled)
            elif status_code >= 400:
                _ACCESS_LOG.warning(msg, extra=extra)
            elif route_path in _PROBE_ROUTES:
                _ACCESS_LOG.debug(msg, extra=extra)
            else:
                _ACCESS_LOG.info(msg, extra=extra)
