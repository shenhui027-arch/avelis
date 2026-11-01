"""FastAPI 应用：REST + OpenAPI（/api/openapi.json），前端只通过 /api 访问。"""

from __future__ import annotations

import re
from functools import lru_cache

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match

from avelis.api import (
    analysis,
    ask,
    auth,
    catalog,
    datasources,
    governance,
    history,
    imports,
    model_settings,
    workbench,
)
from avelis.cache import LockBusy, client as cache_client
from avelis.config import get_settings
from avelis.db import get_engine
from avelis.engines import CapabilityMissing, CredentialMissing, UnsupportedEngine
from avelis.governance.errors import GovernanceError
from avelis.i18n import LocaleMiddleware, gettext, translate


class _FrontendRoute(APIRoute):
    def matches(self, scope):
        path = scope.get("path", "")
        root = scope.get("root_path", "")
        if root and path.startswith(root):  # 与 Starlette 路由一致：挂在路径前缀下时按去掉前缀后的路径判断
            path = path[len(root):]
        if path == "/api" or path.startswith("/api/"):
            return Match.NONE, {}
        return super().matches(scope)


def create_app() -> FastAPI:
    settings = get_settings()
    frontend_dir = settings.frontend_dir
    if frontend_dir is not None:
        frontend_dir = frontend_dir.resolve()
        index = frontend_dir / "index.html"
        if not frontend_dir.is_dir() or not index.is_file() or not index.resolve().is_relative_to(frontend_dir):
            raise ValueError(f"前端目录必须存在且包含目录内的 index.html：{frontend_dir}")
    app = FastAPI(title="Avelis API", version="0.1.0", openapi_url="/api/openapi.json", docs_url="/api/docs",
                  redoc_url=None)
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=False,
                       allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                       allow_headers=["Authorization", "Content-Type", "Accept-Language"])
    app.add_middleware(LocaleMiddleware)

    @app.exception_handler(GovernanceError)
    async def governance_error(_: Request, exc: GovernanceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": translate(exc.message), **exc.detail})

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        return await http_exception_handler(request, StarletteHTTPException(
            exc.status_code, translate(exc.detail), headers=exc.headers))

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

    for module in (auth, datasources, catalog, governance, history, ask, analysis, model_settings, imports, workbench):
        app.include_router(module.router)

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

        app.router.add_api_route("/{path:path}", frontend, methods=["GET", "HEAD"], include_in_schema=False,
                                 route_class_override=_FrontendRoute)
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
