"""Prometheus /metrics 采集端点与共享应用会话解析辅助。"""

from __future__ import annotations

import hmac
import inspect
from contextlib import contextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from avelis.api.deps import get_session, try_authenticated_user
from avelis.config import Settings
from avelis.db import session_factory
from avelis.governance import access
from avelis.observability import METRICS


@contextmanager
def request_session(fastapi_app: FastAPI):
    """在自定义 ASGI 中间件或未注入 Depends(get_session) 的端点内安全复用会话或测试覆盖。"""
    override = fastapi_app.dependency_overrides.get(get_session)
    if override is not None:
        res = override()
        if inspect.isgenerator(res):
            session = next(res)
            try:
                yield session
            finally:
                try:
                    next(res)
                except StopIteration:
                    pass
        else:
            yield res
    else:
        with session_factory()() as session:
            yield session


def register_metrics_route(app: FastAPI, settings: Settings) -> None:
    """挂载受 Bearer Token / 管理员会话双层保护的 GET /metrics（Prometheus 文本格式）。"""

    @app.get("/metrics", include_in_schema=False)
    def prometheus_metrics(request: Request) -> PlainTextResponse:
        auth_header = request.headers.get("authorization", "")
        token = settings.metrics_token
        authorized = False
        if token and auth_header.lower().startswith("bearer "):
            authorized = hmac.compare_digest(auth_header[7:].strip(), token)
        if not authorized:
            with request_session(app) as session:
                user = try_authenticated_user(request, session)
                if user is None:
                    raise HTTPException(status_code=401, detail="未登录或登录已过期")
                if "admin" not in access.roles_of(session, user):
                    raise HTTPException(status_code=403, detail="需要管理员权限")
        return PlainTextResponse(
            METRICS.render_prometheus(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )
