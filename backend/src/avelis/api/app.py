"""FastAPI 应用：REST + OpenAPI（/api/openapi.json），前端只通过 /api 访问。"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import lru_cache

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from avelis.api import (
    analysis,
    ask,
    auth,
    catalog,
    data_policy,
    datasources,
    dossier,
    events,
    governance,
    history,
    imports,
    interchange,
    model_settings,
    skills,
    workbench,
)
from avelis.api import (
    settings as system_settings,
)
from avelis.api.deps import ACCESS_COOKIE, try_authenticated_user
from avelis.api.metrics import register_metrics_route, request_session
from avelis.api.schemas import WorkerHealthOut
from avelis.cache import LockBusy, worker_heartbeat_status
from avelis.cache import client as cache_client
from avelis.config import Settings, get_settings
from avelis.db import get_engine
from avelis.engines import CapabilityMissing, CredentialMissing, UnsupportedEngine
from avelis.governance.errors import GovernanceError
from avelis.i18n import LocaleMiddleware, gettext, translate
from avelis.observability import MetricsMiddleware, RequestContextMiddleware, init_tracing

_STRICT_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; font-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'"
)
_DOCS_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: https://fastapi.tiangolo.com https:; font-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'"
)


def _strip_root_path(scope) -> str:
    path = scope.get("path", "")
    root = scope.get("root_path", "")
    if root and (path == root or path.startswith(root + "/")):  # 与 Starlette 一致：只剥除完整路径段
        path = path[len(root) :]
    return path


API_VERSION = "1.2.0"


class SecurityHeadersMiddleware:
    """纯 ASGI 安全响应头中间件：避免缓冲流式响应，同时覆盖 API、静态页与错误响应。"""

    def __init__(self, app, *, docs_enabled: bool, public_url: str):
        self.app = app
        self.docs_enabled = docs_enabled
        self.public_url = public_url

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = _strip_root_path(scope)
        csp = _DOCS_CSP if (self.docs_enabled and path in ("/api/docs", "/api/docs/oauth2-redirect")) else _STRICT_CSP
        use_hsts = scope.get("scheme") == "https" or self.public_url.startswith("https://")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", ()))
                existing = {k.lower() for k, _ in headers}
                defaults = [
                    (b"x-frame-options", b"DENY"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"strict-origin-when-cross-origin"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                    (b"content-security-policy", csp.encode("latin-1")),
                ]
                if use_hsts:
                    defaults.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                for name, value in defaults:
                    if name not in existing:
                        headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class _OAuthConsentPasswordGuardMiddleware:
    """阻断未完成首次改密的用户访问 /oauth/consent*；未登录访问仍交由 SPA 返回 200。"""

    def __init__(self, app, *, fastapi_app: FastAPI):
        self.app = app
        self.fastapi_app = fastapi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = _strip_root_path(scope)
            if path == "/oauth/consent" or path.startswith("/oauth/consent/"):
                request = Request(scope)
                auth_header = request.headers.get("authorization", "")
                has_bearer = auth_header.lower().startswith("bearer ") and bool(auth_header[7:].strip())
                has_cookie = bool(request.cookies.get(ACCESS_COOKIE))
                if has_bearer or has_cookie:
                    with request_session(self.fastapi_app) as session:
                        user = try_authenticated_user(request, session)
                        blocked = bool(user is not None and user.must_change_password)
                    if blocked:
                        resp = JSONResponse(
                            status_code=403,
                            content={
                                "detail": {
                                    "code": "must_change_password",
                                    "detail": gettext("首次登录或密码重置后必须修改密码"),
                                }
                            },
                        )
                        await resp(scope, receive, send)
                        return
        await self.app(scope, receive, send)


class _FrontendRoute(APIRoute):
    def matches(self, scope):
        path = _strip_root_path(scope)
        if path == "/api" or path.startswith("/api/"):
            return Match.NONE, {}
        # 协议地址不能被 SPA 的 index.html 回退吞掉，关闭模块时仍须返回 404。
        if (
            path == "/metrics"
            or path.startswith("/metrics/")
            or path == "/mcp"
            or path.startswith("/mcp/")
            or path.startswith("/.well-known/")
            or (path.startswith("/oauth/") and path != "/oauth/consent")
        ):
            return Match.NONE, {}
        return super().matches(scope)


class DirectPeerCaptureMiddleware:
    """在 ProxyHeadersMiddleware 改写 scope['client'] 之前记录直连 socket 对端地址。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            client = scope.get("client")
            peer_host = client[0] if client else None
            if peer_host and "avelis_direct_peer" not in scope:
                scope["avelis_direct_peer"] = peer_host
        await self.app(scope, receive, send)


def create_app(*, settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    mcp_app = None

    @asynccontextmanager
    async def lifespan(application):
        if mcp_app is None:
            yield
        else:
            # ASGI mount 不会自动进入子应用 lifespan；SDK 的 session manager 在此启动。
            async with mcp_app.router.lifespan_context(mcp_app):
                yield

    frontend_dir = settings.frontend_dir
    if frontend_dir is not None:
        frontend_dir = frontend_dir.resolve()
        index = frontend_dir / "index.html"
        if not frontend_dir.is_dir() or not index.is_file() or not index.resolve().is_relative_to(frontend_dir):
            raise ValueError(f"前端目录必须存在且包含目录内的 index.html：{frontend_dir}")
    app = FastAPI(
        title="Avelis API",
        version=API_VERSION,
        openapi_url="/api/openapi.json" if settings.docs_enabled else None,
        docs_url="/api/docs" if settings.docs_enabled else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(_OAuthConsentPasswordGuardMiddleware, fastapi_app=app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=bool(settings.cors_origins) and "*" not in settings.cors_origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Accept-Language",
            "Last-Event-ID",
            "Cache-Control",
            "X-Request-ID",
            "X-Trace-ID",
            "traceparent",
        ],
        expose_headers=["X-Request-ID", "X-Trace-ID", "traceparent"],
    )
    app.add_middleware(LocaleMiddleware)
    app.add_middleware(
        SecurityHeadersMiddleware,
        docs_enabled=settings.docs_enabled,
        public_url=settings.public_url,
    )
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=settings.forwarded_allow_ips)
    app.add_middleware(DirectPeerCaptureMiddleware)

    @app.exception_handler(GovernanceError)
    async def governance_error(_: Request, exc: GovernanceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": translate(exc.message), **exc.detail})

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        return await http_exception_handler(
            request, StarletteHTTPException(exc.status_code, translate(exc.detail), headers=exc.headers)
        )

    @app.exception_handler(CredentialMissing)
    async def credential_missing(_: Request, exc: CredentialMissing) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": translate(exc.args[0])})

    @app.exception_handler(CapabilityMissing)
    async def capability_missing(_: Request, exc: CapabilityMissing) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": translate(exc.args[0]), "capability": exc.capability})

    @app.exception_handler(UnsupportedEngine)
    async def unsupported_engine(_: Request, exc: UnsupportedEngine) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": translate(exc.args[0])})

    @app.exception_handler(LockBusy)
    async def lock_busy(_: Request, exc: LockBusy) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": gettext("资源正被其他任务占用"), "lock": str(exc)})

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/health/worker", response_model=WorkerHealthOut)
    def worker_health() -> dict:
        # 汇总所有 worker 各自的心跳（#159）；Redis 不可用时按不存活处理
        try:
            alive, last_seen = worker_heartbeat_status(datetime.now(timezone.utc))
        except Exception:
            alive, last_seen = False, None
        return {"alive": alive, "last_seen": last_seen}

    @app.get("/api/health/ready")
    def ready() -> JSONResponse:
        failed = []
        try:
            with get_engine().connect() as connection:
                if connection.scalar(text("SELECT 1")) != 1:
                    failed.append("database")
        except Exception:
            failed.append("database")
        try:
            if not cache_client().ping():
                failed.append("redis")
        except Exception:
            failed.append("redis")
        if failed:
            return JSONResponse(status_code=503, content={"status": "error", "failed": failed})
        return JSONResponse(content={"status": "ok"})

    if settings.metrics_enabled:
        register_metrics_route(app, settings)

    for module in (
        auth,
        datasources,
        events,
        catalog,
        data_policy,
        dossier,
        governance,
        history,
        ask,
        analysis,
        model_settings,
        system_settings,
        imports,
        interchange,
        skills,
        workbench,
    ):
        app.include_router(module.router)

    if settings.mcp_enabled:
        from avelis.mcp.server import create_mcp_app, protected_resource_routes
        from avelis.oauth.router import create_oauth_router

        app.include_router(create_oauth_router(settings))
        app.router.routes.extend(protected_resource_routes(settings))
        mcp_app = create_mcp_app(settings)

    if frontend_dir is not None:

        def frontend(path: str) -> FileResponse:
            try:
                target = (frontend_dir / path).resolve()
            except (OSError, RuntimeError, ValueError):
                raise HTTPException(status_code=404, detail="未找到") from None
            if not target.is_relative_to(frontend_dir):
                raise HTTPException(status_code=404, detail="未找到")
            if not target.is_file():
                return FileResponse(index, headers={"Cache-Control": "no-cache"})
            headers = {}
            if target.name == "index.html":
                headers["Cache-Control"] = "no-cache"
            elif path.startswith("assets/") and re.search(r"-[A-Za-z0-9_-]{3,}\.", target.name):
                headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return FileResponse(target, headers=headers)

        app.router.add_api_route(
            "/{path:path}",
            frontend,
            methods=["GET", "HEAD"],
            include_in_schema=False,
            route_class_override=_FrontendRoute,
        )
    if mcp_app is not None:
        # 只交给 SDK 处理协议路径，避免根 mount 接走主应用的 JSON 404/405。
        app.add_route("/mcp", mcp_app, methods=["GET", "POST", "DELETE"])
    init_tracing(app, settings)
    return app


@lru_cache(maxsize=1)
def _application() -> FastAPI:
    return create_app()


async def app(scope, receive, send) -> None:
    # 首次 ASGI 启动时才读配置，导入模块和生成接口结构都不触发启动副作用。
    try:
        application = _application()
    except Exception as exc:
        if scope["type"] == "lifespan":
            await receive()
            await send({"type": "lifespan.startup.failed", "message": str(exc)})
        raise
    await application(scope, receive, send)
