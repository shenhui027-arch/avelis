"""FastAPI 应用：REST + OpenAPI（/api/openapi.json），前端只通过 /api 访问。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from avelis.api import analysis, ask, auth, catalog, datasources, governance, imports, model_settings, workbench
from avelis.cache import LockBusy
from avelis.config import get_settings
from avelis.engines import CapabilityMissing, CredentialMissing, UnsupportedEngine
from avelis.governance.errors import GovernanceError


def create_app() -> FastAPI:
    app = FastAPI(title="Avelis API", version="0.1.0", openapi_url="/api/openapi.json", docs_url="/api/docs",
                  redoc_url=None)
    app.add_middleware(CORSMiddleware, allow_origins=list(get_settings().cors_origins), allow_credentials=False,
                       allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                       allow_headers=["Authorization", "Content-Type"])

    @app.exception_handler(GovernanceError)
    async def governance_error(_: Request, exc: GovernanceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message, **exc.detail})

    @app.exception_handler(CredentialMissing)
    async def credential_missing(_: Request, exc: CredentialMissing) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(CapabilityMissing)
    async def capability_missing(_: Request, exc: CapabilityMissing) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc), "capability": exc.capability})

    @app.exception_handler(UnsupportedEngine)
    async def unsupported_engine(_: Request, exc: UnsupportedEngine) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(LockBusy)
    async def lock_busy(_: Request, exc: LockBusy) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": "资源正被其他任务占用", "lock": str(exc)})

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    for module in (auth, datasources, catalog, governance, ask, analysis, model_settings, imports, workbench):
        app.include_router(module.router)
    return app


app = create_app()
