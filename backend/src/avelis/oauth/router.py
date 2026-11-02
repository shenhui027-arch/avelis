"""OAuth 协议路由与使用现有 Bearer 登录的授权确认/管理 API。"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlsplit

from authlib.common.security import generate_token
from authlib.oauth2.rfc6749 import InvalidClientError, InvalidRequestError, OAuth2Error
from authlib.oauth2.rfc9207 import IssuerParameter
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.datastructures import QueryParams
from starlette.responses import JSONResponse, RedirectResponse

from avelis import audit
from avelis.api.deps import current_user, get_session
from avelis.config import Settings
from avelis.governance import access
from avelis.i18n import gettext
from avelis.models import Domain, OAuthClient, OAuthConsentTicket, OAuthGrant, User

from .adapter import FastAPIAuthorizationServer, FastAPIJsonRequest, FastAPIOAuth2Request
from .cimd import CIMDError, fetch_client_metadata
from .endpoints import RegistrationEndpoint, RevokeEndpoint, authorization_metadata
from .grants import CodeGrant, RefreshGrant, ResourceBinding, S256CodeChallenge
from .repository import Repository, aware, credential_hash, now

SCOPE_DESCRIPTIONS = {
    "metrics:read": "检索与读取已发布口径定义 / Read published metric definitions",
    "lineage:read": "读取可访问资产的血缘与变更 / Read permitted lineage and changes",
    "ask:execute": "按受治理的口径查询数据 / Query data through governed metrics",
}


class ConsentScopeOut(BaseModel):
    scope: str
    description: str


class ConsentDomainOut(BaseModel):
    id: str
    code: str
    name: str


class ConsentOut(BaseModel):
    client_name: str
    client_id: str
    source: str
    scopes: list[ConsentScopeOut]
    domains: list[ConsentDomainOut]


class RedirectOut(BaseModel):
    redirect_to: str


class AuthorizationOut(BaseModel):
    id: str
    client_id: str
    client_name: str
    source: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class ClientOut(BaseModel):
    id: str
    client_id: str
    client_name: str
    source: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None
    disabled_at: datetime | None
    authorization_count: int


class OkOut(BaseModel):
    ok: bool


async def resolve_client(repository: Repository, client_id: str, settings: Settings):
    client = repository.get_client(client_id)
    if client and (client.disabled_at is not None or client.source != "cimd"):
        return client
    if client and client.metadata_expires_at and now() < aware(client.metadata_expires_at):
        return client
    try:
        metadata_scheme = urlsplit(client_id).scheme
    except ValueError as exc:
        raise InvalidClientError("Invalid client metadata URL.") from exc
    if metadata_scheme not in ("http", "https"):
        return client
    try:
        metadata = await fetch_client_metadata(client_id, local_urls=frozenset(settings.mcp_cimd_allowed_urls))
    except CIMDError as exc:
        raise InvalidClientError("Client metadata could not be validated.") from exc
    if not set(metadata.get("scope", " ".join(SCOPE_DESCRIPTIONS)).split()) <= set(SCOPE_DESCRIPTIONS):
        raise InvalidClientError("Client metadata requests unsupported scopes.")
    timestamp = now()
    # 网络拉取不持锁。写回时重新加锁，防止覆盖管理员在等待期间的禁用。
    client = repository.get_client(client_id, lock=True)
    if client is not None and client.disabled_at is not None:
        return client
    new = client is None
    if new:
        client = OAuthClient(client_id=client_id, source="cimd")
    client.client_name = metadata.get("client_name", "OAuth client")
    client.redirect_uris = metadata["redirect_uris"]
    client.scope = metadata.get("scope", " ".join(SCOPE_DESCRIPTIONS))
    client.grant_types = metadata.get("grant_types", ["authorization_code", "refresh_token"])
    client.response_types = metadata.get("response_types", ["code"])
    client.token_endpoint_auth_method = "none"
    client.metadata_hash = hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest()
    client.metadata_fetched_at = timestamp
    client.metadata_expires_at = timestamp + timedelta(seconds=settings.mcp_cimd_cache_ttl_seconds)
    if new:
        try:
            # 保存点仅处理首次并发拉取的唯一键竞争，不回滚请求的其他工作。
            with repository.session.begin_nested():
                repository.session.add(client)
                repository.session.flush()
        except IntegrityError:
            client = repository.get_client(client_id, lock=True)
        else:
            audit.record(
                repository.session,
                None,
                "oauth.client_register",
                "oauth_client",
                client.id,
                client_id=client_id,
                source="cimd",
            )
    return client


def create_oauth_router(settings: Settings) -> APIRouter:
    # Authlib 的 DEBUG 含原始凭据，覆盖其已加载 logger 的级别。
    for name in tuple(logging.Logger.manager.loggerDict):
        if name == "authlib" or name.startswith("authlib."):
            logging.getLogger(name).setLevel(logging.INFO)
    logging.getLogger("authlib").setLevel(logging.INFO)
    metadata = authorization_metadata(settings)
    router = APIRouter(tags=["OAuth"])

    class Issuer(IssuerParameter):
        def get_issuer(self):
            return settings.public_url

    def server_for(session):
        server = FastAPIAuthorizationServer(Repository(session), settings)
        server.metadata = metadata
        server.register_grant(CodeGrant, [S256CodeChallenge(required=True), ResourceBinding()])
        server.register_grant(RefreshGrant)
        server.register_endpoint(RevokeEndpoint)
        server.register_endpoint(RegistrationEndpoint)
        server.register_extension(Issuer())
        return server

    def finish(session, response):
        session.commit()
        return response

    def ticket_row(session, ticket):
        row = session.scalar(
            select(OAuthConsentTicket).where(OAuthConsentTicket.ticket_hash == credential_hash(ticket))
        )
        if not row or row.consumed_at is not None or now() >= aware(row.expires_at):
            raise HTTPException(400, "授权确认票据无效或已过期")
        return row

    def restore_request(row):
        pairs = [tuple(pair) for pair in row.parameters]
        query = urlencode(pairs)
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "https",
                "server": ("localhost", 443),
                "path": "/oauth/authorize",
                "query_string": query.encode(),
                "headers": [],
            }
        )
        adapted = FastAPIOAuth2Request(request)
        adapted._args = QueryParams(pairs)
        return adapted

    def client_for_ticket(session, row):
        client = session.get(OAuthClient, row.client_record_id)
        if client is None or client.disabled_at:
            raise HTTPException(400, "OAuth 客户端已停用")
        return client

    def consent_grant(server, adapted, user):
        try:
            return server.get_consent_grant(adapted, end_user=user.id)
        except OAuth2Error as error:
            raise HTTPException(400, error.description) from error

    @router.get("/.well-known/oauth-authorization-server", include_in_schema=False)
    def discovery():
        return dict(metadata)

    @router.get("/oauth/authorize", include_in_schema=False)
    async def authorize(request: Request, session: Session = Depends(get_session)):
        server = server_for(session)
        adapted = FastAPIOAuth2Request(request)
        try:
            adapted.reject_duplicate_parameters()
            if adapted.payload.client_id:
                await resolve_client(server.repository, adapted.payload.client_id, settings)
            # 不让未知 client 或不精确的 redirect_uri 获得重定向。
            client = server.query_client(adapted.payload.client_id)
            if not client:
                raise InvalidClientError("Unknown or disabled client.")
            if not adapted.payload.redirect_uri or not client.check_redirect_uri(adapted.payload.redirect_uri):
                raise InvalidRequestError("Invalid redirect_uri.")
            server.get_consent_grant(adapted, end_user=None)
        except OAuth2Error as error:
            response = server.handle_error_response(adapted, error)
            Issuer().add_issuer_parameter(server, response)
            return finish(session, response)
        raw_ticket = generate_token(48)
        # 冻结实际解析的 scope；客户端默认权限随后扩大不能改变待确认票据。
        parameters = [[key, value] for key, value in request.query_params.multi_items() if key != "scope"]
        parameters.append(["scope", adapted.scope or ""])
        session.add(
            OAuthConsentTicket(
                ticket_hash=credential_hash(raw_ticket),
                client_record_id=client.id,
                parameters=parameters,
                expires_at=now() + timedelta(seconds=300),
            )
        )
        return finish(
            session,
            RedirectResponse(
                settings.public_url + "/oauth/consent?" + urlencode({"ticket": raw_ticket}), status_code=302
            ),
        )

    @router.post("/oauth/token", include_in_schema=False)
    async def token(request: Request, session: Session = Depends(get_session)):
        server = server_for(session)
        adapted = FastAPIOAuth2Request(request, await request.form())
        try:
            adapted.reject_duplicate_parameters()
            if adapted.payload.client_id:
                await resolve_client(server.repository, adapted.payload.client_id, settings)
        except OAuth2Error as error:
            return finish(session, server.handle_error_response(adapted, error))
        return finish(session, server.create_token_response(adapted))

    @router.post("/oauth/revoke", include_in_schema=False)
    async def revoke(request: Request, session: Session = Depends(get_session)):
        server = server_for(session)
        adapted = FastAPIOAuth2Request(request, await request.form())
        try:
            adapted.reject_duplicate_parameters()
        except OAuth2Error as error:
            return finish(session, server.handle_error_response(adapted, error))
        return finish(session, server.create_endpoint_response("revocation", adapted))

    @router.post("/oauth/register", include_in_schema=False)
    async def register(request: Request, session: Session = Depends(get_session)):
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
        server = server_for(session)
        return finish(
            session, server.create_endpoint_response("client_registration", FastAPIJsonRequest(request, body))
        )

    @router.get("/api/oauth/consent/{ticket}", response_model=ConsentOut)
    def consent(ticket: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
        row = ticket_row(session, ticket)
        client = client_for_ticket(session, row)
        adapted = restore_request(row)
        consent_grant(server_for(session), adapted, user)
        scopes = (adapted.scope if adapted.scope is not None else client.scope).split()
        domains = session.scalars(
            select(Domain).where(Domain.id.in_(access.domain_ids(session, user))).order_by(Domain.code)
        )
        return {
            "client_name": client.client_name,
            "client_id": client.client_id,
            "source": client.source,
            "scopes": [{"scope": scope, "description": gettext(SCOPE_DESCRIPTIONS[scope])} for scope in scopes],
            "domains": [{"id": d.id, "name": d.name, "code": d.code} for d in domains],
        }

    def decide(ticket, user, session, approve):
        row = ticket_row(session, ticket)
        client_for_ticket(session, row)
        server = server_for(session)
        adapted = restore_request(row)
        # 与签发/撤销共用客户端锁，不能在撤销后签发授权码。
        grant = consent_grant(server, adapted, user)
        consumed = session.execute(
            update(OAuthConsentTicket)
            .where(
                OAuthConsentTicket.id == row.id,
                OAuthConsentTicket.consumed_at.is_(None),
                OAuthConsentTicket.expires_at > now(),
            )
            .values(consumed_at=now())
        ).rowcount
        if consumed != 1:
            raise HTTPException(400, "授权确认票据无效或已过期")
        if approve:
            authorization = OAuthGrant(
                client_record_id=adapted.client.id,
                user_id=user.id,
                scope=adapted.scope if adapted.scope is not None else adapted.client.scope,
            )
            session.add(authorization)
            session.flush()
            adapted.grant_id = authorization.id
            audit.record(
                session,
                user,
                "oauth.authorize",
                "oauth_grant",
                authorization.id,
                client_id=adapted.client.client_id,
                scopes=authorization.scope.split(),
            )
        else:
            audit.record(
                session, user, "oauth.deny", "oauth_client", adapted.client.id, client_id=adapted.client.client_id
            )
        response = server.create_authorization_response(adapted, grant_user=user.id if approve else None, grant=grant)
        if response.status_code != 302:
            raise HTTPException(400, "OAuth 授权请求无效")
        session.commit()
        return {"redirect_to": response.headers["location"]}

    @router.post("/api/oauth/consent/{ticket}/approve", response_model=RedirectOut)
    def approve(ticket: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
        return decide(ticket, user, session, True)

    @router.post("/api/oauth/consent/{ticket}/deny", response_model=RedirectOut)
    def deny(ticket: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
        return decide(ticket, user, session, False)

    @router.get("/api/oauth/authorizations", response_model=list[AuthorizationOut])
    def authorizations(user: User = Depends(current_user), session: Session = Depends(get_session)):
        rows = session.execute(
            select(OAuthGrant, OAuthClient)
            .join(OAuthClient, OAuthGrant.client_record_id == OAuthClient.id)
            .where(OAuthGrant.user_id == user.id)
            .order_by(OAuthGrant.created_at.desc())
        )
        return [
            {
                "id": grant.id,
                "client_id": client.client_id,
                "client_name": client.client_name,
                "source": client.source,
                "scopes": grant.scope.split(),
                "created_at": grant.created_at,
                "last_used_at": grant.last_used_at,
                "revoked_at": grant.revoked_at,
            }
            for grant, client in rows
        ]

    @router.delete("/api/oauth/authorizations/{authorization_id}", response_model=OkOut)
    def revoke_authorization(
        authorization_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)
    ):
        grant = session.get(OAuthGrant, authorization_id)
        if grant is None or grant.user_id != user.id:
            raise HTTPException(404, "授权记录不存在")
        client = session.get(OAuthClient, grant.client_record_id)
        repository = Repository(session)
        repository.get_client(client.client_id, lock=True)
        repository.revoke_grant(grant)
        audit.record(session, user, "oauth.revoke", "oauth_grant", grant.id, client_id=client.client_id)
        session.commit()
        return {"ok": True}

    @router.get("/api/oauth/clients", response_model=list[ClientOut])
    def clients(user: User = Depends(current_user), session: Session = Depends(get_session)):
        access.require_role(session, user, "admin")
        counts = dict(
            session.execute(
                select(OAuthGrant.client_record_id, func.count(func.distinct(OAuthGrant.user_id)))
                .where(OAuthGrant.revoked_at.is_(None))
                .group_by(OAuthGrant.client_record_id)
            ).all()
        )
        return [
            {
                "id": c.id,
                "client_id": c.client_id,
                "client_name": c.client_name,
                "source": c.source,
                "scopes": c.scope.split(),
                "created_at": c.created_at,
                "last_used_at": c.last_used_at,
                "disabled_at": c.disabled_at,
                "authorization_count": counts.get(c.id, 0),
            }
            for c in session.scalars(select(OAuthClient).order_by(OAuthClient.created_at.desc()))
        ]

    @router.post("/api/oauth/clients/{client_record_id}/revoke", response_model=OkOut)
    def disable_client(
        client_record_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)
    ):
        access.require_role(session, user, "admin")
        client = session.get(OAuthClient, client_record_id)
        if not client:
            raise HTTPException(404, "OAuth 客户端不存在")
        repository = Repository(session)
        repository.get_client(client.client_id, lock=True)
        repository.disable_client(client)
        audit.record(session, user, "oauth.client_disable", "oauth_client", client.id, client_id=client.client_id)
        session.commit()
        return {"ok": True}

    return router
