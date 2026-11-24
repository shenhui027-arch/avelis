"""SDK 身份映射、逐工具 scope 与只含摘要的调用审计。"""

from __future__ import annotations

import json
from contextvars import ContextVar
from urllib.parse import urlsplit

from fastapi import HTTPException
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware, get_access_token
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser, BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from sqlalchemy import update
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from avelis import audit
from avelis.db import utcnow
from avelis.models import OAuthClient, OAuthGrant, User
from avelis.oauth.repository import Repository
from avelis.oauth.urls import request_settings, resource_metadata_url

DEFAULT_PROTOCOL_VERSION = "2025-03-26"
SUPPORTED_HANDSHAKE_VERSIONS = ("2025-03-26", "2025-06-18", "2025-11-25")
SUPPORTED_PROTOCOL_VERSIONS = (*SUPPORTED_HANDSHAKE_VERSIONS, "2026-07-28")
PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[-1]
MAX_BODY_BYTES = 1024 * 1024
TOOL_SCOPES = {
    "search_metrics": "metrics:read",
    "get_metric": "metrics:read",
    "ask_metric": "ask:execute",
    "lineage_neighbors": "lineage:read",
    "list_changes": "lineage:read",
}
SCOPES = tuple(dict.fromkeys(TOOL_SCOPES.values()))
audited_tool_call: ContextVar[tuple[AccessToken, str] | None] = ContextVar("mcp_audited_tool_call", default=None)
ARGUMENT_NAMES = frozenset(
    {
        "keyword",
        "domain_id",
        "limit",
        "metric",
        "metrics",
        "order",
        "dimensions",
        "start_date",
        "end_date",
        "filters",
        "compare",
        "compare_start",
        "compare_end",
        "time_grain",
        "asset_id",
        "column",
        "direction",
        "depth",
        "object_type",
        "object_id",
    }
)


class StoredTokenVerifier(TokenVerifier):
    def __init__(self, session_maker, settings):
        self.session_maker, self.settings = session_maker, settings

    async def verify_token(self, token: str) -> AccessToken | None:
        with self.session_maker() as session:
            record = Repository(session).verify_access_token(token, self.settings.mcp_resource_url)
            if record is None:
                return None
            return AccessToken(
                token=token,
                client_id=record.client_id,
                subject=record.user_id,
                scopes=record.scope.split(),
                resource=record.resource,
                expires_at=int(record.expires_at.timestamp()),
                claims={"iss": self.settings.public_url, "token_id": record.id, "grant_id": record.grant_id},
            )


class RequestAuthMiddleware:
    """复用 SDK 认证中间件，但令资源校验与 challenge 使用当前请求的地址。"""

    def __init__(self, app, settings, session_maker):
        self.app, self.settings, self.session_maker = app, settings, session_maker

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path, root = scope.get("path", ""), scope.get("root_path", "")
        if root and (path == root or path.startswith(root + "/")):
            path = path[len(root) :]
        if path != "/mcp":
            return await self.app(scope, receive, send)
        request = Request(scope)
        try:
            current = request_settings(self.settings, request)
        except HTTPException as error:
            return await JSONResponse({"detail": error.detail}, status_code=error.status_code)(scope, receive, send)
        # 与显式配置时相同的 Origin 检查；推导模式允许合法 Host，但 Origin 必须同源。
        origin = urlsplit(current.public_url)
        security = TransportSecurityMiddleware(
            TransportSecuritySettings(
                allowed_hosts=[origin.netloc if self.settings.public_url else request.headers["host"]],
                allowed_origins=[f"{origin.scheme}://{origin.netloc}"],
            )
        )
        rejected = await security.validate_request(request)
        if rejected is not None:
            return await rejected(scope, receive, send)
        protected = RequireAuthMiddleware(self.app, [], resource_metadata_url(self.settings, request))
        authenticated = AuthenticationMiddleware(
            AuthContextMiddleware(protected),
            # Repository 执行逐字 resource 比对；不重复 SDK 的宽松 URL 比对。
            backend=BearerAuthBackend(StoredTokenVerifier(self.session_maker, current)),
        )
        return await authenticated(scope, receive, send)


def argument_summary(arguments) -> dict:
    """不存值：过滤值和自由文本可能包含敏感数据，未知字段名也可能被用来夹带凭据。"""
    if not isinstance(arguments, dict):
        return {"invalid_shape": True}
    return {
        "argument_names": sorted(name for name in arguments if name in ARGUMENT_NAMES),
        "unknown_argument_count": sum(name not in ARGUMENT_NAMES for name in arguments),
        "dimensions_count": len(arguments["dimensions"]) if isinstance(arguments.get("dimensions"), list) else 0,
        "filters_count": len(arguments["filters"]) if isinstance(arguments.get("filters"), list) else 0,
    }


def audit_tool_call(session_maker, token: AccessToken, name: str, arguments) -> None:
    with session_maker() as session:
        user = session.get(User, token.subject)
        audit.record(
            session,
            user,
            "mcp." + name,
            "mcp_tool",
            name,
            client_id=token.client_id,
            token_id=token.claims.get("token_id"),
            parameters=argument_summary(arguments),
        )
        used_at = utcnow()
        session.execute(
            update(OAuthClient).where(OAuthClient.client_id == token.client_id).values(last_used_at=used_at)
        )
        session.execute(
            update(OAuthGrant).where(OAuthGrant.id == token.claims["grant_id"]).values(last_used_at=used_at)
        )
        session.commit()


def _unsupported_protocol_response(description: str) -> JSONResponse:
    return JSONResponse(
        {
            "error": "unsupported_protocol_version",
            "error_description": description,
            "supported_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
        },
        status_code=400,
    )


class ToolScopeGate:
    """SDK 认证之后、dispatch 之前检查 body 工具名；复用 SDK 的 HTTP challenge。"""

    def __init__(self, app, settings, session_maker, tool_arguments):
        self.app, self.settings, self.session_maker = app, settings, session_maker
        self.tool_arguments = tool_arguments

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        root = scope.get("root_path", "")
        if root and (path == root or path.startswith(root + "/")):
            path = path[len(root) :]
        if scope["type"] != "http" or path != "/mcp" or not isinstance(scope.get("user"), AuthenticatedUser):
            return await self.app(scope, receive, send)
        request = Request(scope, receive)
        header_values = request.headers.getlist("mcp-protocol-version")
        if len(header_values) > 1:
            return await _unsupported_protocol_response("Duplicate MCP-Protocol-Version header")(scope, receive, send)
        raw_version = header_values[0].strip() if header_values else None
        if raw_version is not None and raw_version not in SUPPORTED_PROTOCOL_VERSIONS:
            return await _unsupported_protocol_response(f"Unsupported MCP protocol version: {raw_version}")(
                scope, receive, send
            )
        if raw_version is not None and raw_version != header_values[0]:
            normalized_pv = raw_version.encode("ascii")
            scope = {
                **scope,
                "headers": [
                    (key, normalized_pv if key.lower() == b"mcp-protocol-version" else value)
                    for key, value in scope["headers"]
                ],
            }
            request = Request(scope, receive)

        def make_versioned_send(negotiated: str | None):
            async def versioned_send(msg):
                if (
                    negotiated is not None
                    and msg["type"] == "http.response.start"
                    and not any(key.lower() == b"mcp-protocol-version" for key, _ in msg["headers"])
                ):
                    msg = {
                        **msg,
                        "headers": [*msg["headers"], (b"mcp-protocol-version", negotiated.encode("ascii"))],
                    }
                await send(msg)

            return versioned_send

        if request.method != "POST":
            negotiated_version = raw_version or DEFAULT_PROTOCOL_VERSION
            return await self.app(scope, receive, make_versioned_send(negotiated_version))
        body = await request.body()
        try:
            message = json.loads(body)
        except (ValueError, RecursionError):
            message = None
        if isinstance(message, dict) and message.get("method") == "initialize" and raw_version != PROTOCOL_VERSION:
            params = message.get("params")
            req_pv = params.get("protocolVersion") if isinstance(params, dict) else None
            if req_pv in SUPPORTED_HANDSHAKE_VERSIONS:
                negotiated_version = req_pv
            elif req_pv == "2024-11-05":
                negotiated_version = None
            else:
                negotiated_version = SUPPORTED_HANDSHAKE_VERSIONS[-1]
        elif raw_version is not None:
            negotiated_version = raw_version
        else:
            negotiated_version = DEFAULT_PROTOCOL_VERSION
        versioned_send = make_versioned_send(negotiated_version)

        required = None
        audited = None
        if isinstance(message, dict) and message.get("method") == "tools/call":
            params = message.get("params")
            if isinstance(params, dict) and isinstance(params.get("name"), str):
                name = params["name"]
                required = TOOL_SCOPES.get(name)
                token = get_access_token()
                if required and token:
                    audit_tool_call(self.session_maker, token, name, params.get("arguments", {}))
                    audited = (token, name)

        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        async def challenge_send(msg):
            if msg["type"] == "http.response.start" and msg["status"] == 403:
                msg["headers"] = [
                    (key, value + f', scope="{required}"'.encode() if key == b"www-authenticate" else value)
                    for key, value in msg["headers"]
                ]
            await versioned_send(msg)

        if required:
            arguments = message["params"].get("arguments", {})
            if isinstance(arguments, dict) and arguments.keys() - self.tool_arguments[name]:
                return await JSONResponse(
                    {"error": "invalid_request", "error_description": "Unknown tool argument"}, status_code=400
                )(scope, receive, versioned_send)
            # 保留 dispatch 前的审计（含参数校验失败），包装层仅补记绕过门禁的调用。
            context = audited_tool_call.set(audited)
            try:
                return await RequireAuthMiddleware(
                    self.app,
                    [required],
                    resource_metadata_url(self.settings, request),
                )(scope, replay, challenge_send)
            finally:
                audited_tool_call.reset(context)
        return await self.app(scope, replay, versioned_send)
