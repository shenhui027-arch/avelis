"""MCP SDK 无状态 HTTP 子应用；宿主负责根挂载并进入本应用 lifespan。"""

from __future__ import annotations

import inspect
import json
from functools import wraps
from typing import get_type_hints
from urllib.parse import urlsplit

from mcp.server.auth.handlers.metadata import ProtectedResourceMetadataHandler
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.routes import build_resource_metadata_url, cors_middleware
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import RequestBodyLimitMiddleware, TransportSecuritySettings
from mcp.shared.auth import ProtectedResourceMetadata
from pydantic import AnyHttpUrl
from starlette.middleware import Middleware
from starlette.routing import Route

from avelis import db
from avelis.governance.errors import GovernanceError
from avelis.i18n import gettext, translate
from avelis.mcp.auth import (
    MAX_BODY_BYTES,
    SCOPES,
    TOOL_SCOPES,
    RequestAuthMiddleware,
    ToolScopeGate,
    audit_tool_call,
    audited_tool_call,
)
from avelis.mcp.tools import ToolService
from avelis.models import User
from avelis.oauth.urls import request_settings

DESCRIPTIONS = {
    "search_metrics": "检索有权访问的已发布口径摘要。Search published metric metadata.",
    "get_metric": "读取口径完整定义、版本与依赖资产。Read a governed metric definition.",
    "ask_metric": (
        "按结构化意图编译并执行受治理的问数，默认最多 200 行且不超过页面上限。"
        "Execute a governed metric query; SQL input is not accepted."
    ),
    "lineage_neighbors": "读取有权访问的资产或列的上下游血缘。Read authorized lineage neighbors.",
    "list_changes": "读取资产或口径近期的结构变化与影响。Read authorized structural changes and impacts.",
}


def protected_resource_routes(settings):
    async def metadata(request):
        current = request_settings(settings, request)
        return await ProtectedResourceMetadataHandler(
            ProtectedResourceMetadata(
                resource=current.mcp_resource_url,
                authorization_servers=[current.public_url],
                scopes_supported=list(SCOPES),
            )
        ).handle(request)

    path = (
        urlsplit(str(build_resource_metadata_url(AnyHttpUrl(settings.mcp_resource_url)))).path
        if settings.public_url
        else "/.well-known/oauth-protected-resource/mcp"
    )
    return [Route(path, endpoint=cors_middleware(metadata, ["GET", "OPTIONS"]), methods=["GET", "OPTIONS"])]


def create_mcp_app(settings, session_maker=None):
    session_maker = session_maker or (lambda: db.session_factory()())
    server = MCPServer(
        "Avelis",
        version="1.0.1",
        subscriptions=False,
    )
    tool_arguments = {}

    def register(name):
        method = getattr(ToolService, name)
        signature = inspect.signature(method, eval_str=True)
        parameters = [p for key, p in signature.parameters.items() if key != "self"]
        tool_arguments[name] = {p.name for p in parameters}

        @wraps(method)
        def invoke(**arguments):
            token = get_access_token()
            if token is None:
                raise ToolError("OAuth authentication context missing")
            if audited_tool_call.get() != (token, name):
                audit_tool_call(session_maker, token, name, arguments)
            required = TOOL_SCOPES[name]
            if required not in token.scopes:
                raise ToolError(
                    json.dumps({"error": "insufficient_scope", "error_description": f"Required scope: {required}"})
                )
            with session_maker() as session:
                user = session.get(User, token.subject)
                if user is None or not user.is_active:
                    raise ToolError(gettext("用户不存在或已停用"))
                try:
                    result = getattr(ToolService(session, user, settings), name)(**arguments)
                    session.commit()
                    return result
                except GovernanceError as exc:
                    session.rollback()
                    raise ToolError(translate(exc.message)) from exc

        invoke.__signature__ = signature.replace(parameters=parameters)
        invoke.__annotations__ = get_type_hints(method)
        server.add_tool(invoke, name=name, description=DESCRIPTIONS[name], annotations={"readOnlyHint": True})

    for name in TOOL_SCOPES:
        register(name)
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        max_request_body_size=MAX_BODY_BYTES,
        # 地址和 Origin 在 RequestAuthMiddleware 中逐请求校验，SDK 仍检查协议和 Content-Type。
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    app.user_middleware.extend(
        [
            Middleware(RequestBodyLimitMiddleware, max_body_size=MAX_BODY_BYTES),
            Middleware(RequestAuthMiddleware, settings=settings, session_maker=session_maker),
            Middleware(ToolScopeGate, settings=settings, session_maker=session_maker, tool_arguments=tool_arguments),
        ]
    )
    app.state.mcp_server = server
    return app
