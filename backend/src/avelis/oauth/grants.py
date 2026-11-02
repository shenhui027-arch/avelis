"""OAuth grants、PKCE、精确资源绑定与重放处置。"""

from __future__ import annotations

from datetime import timedelta

from authlib.oauth2.rfc6749 import InvalidGrantError, InvalidRequestError, OAuth2Error
from authlib.oauth2.rfc6749.grants import AuthorizationCodeGrant, RefreshTokenGrant
from authlib.oauth2.rfc7636 import CodeChallenge
from sqlalchemy import select

from avelis.db import new_id
from avelis.models import OAuthAuthorizationCode

from .repository import aware, credential_hash, now


class InvalidTargetError(OAuth2Error):
    error = "invalid_target"
    description = "Exactly this MCP resource must be requested."


def require_resource(request, expected, bound=None):
    values = request.payload.datalist.get("resource", [])
    if len(values) != 1 or values[0] != expected or (bound is not None and values[0] != bound):
        raise InvalidTargetError()


class S256CodeChallenge(CodeChallenge):
    SUPPORTED_CODE_CHALLENGE_METHOD = ["S256"]

    def validate_code_challenge(self, grant, redirect_uri):
        super().validate_code_challenge(grant, redirect_uri)
        if not grant.request.payload.data.get("code_challenge"):
            raise InvalidRequestError("'code_challenge' is required.")
        if grant.request.payload.data.get("code_challenge_method") != "S256":
            raise InvalidRequestError("Only 'S256' is accepted.")


class ResourceBinding:
    def __call__(self, grant):
        grant.register_hook("after_validate_authorization_request_payload", self.authorize)
        grant.register_hook("after_validate_token_request", self.token)

    def authorize(self, grant, redirect_uri):
        require_resource(grant.request, grant.server.settings.public_url + "/mcp")

    def token(self, grant, result):
        require_resource(
            grant.request, grant.server.settings.public_url + "/mcp", grant.request.authorization_code.resource
        )


class CodeGrant(AuthorizationCodeGrant):
    TOKEN_ENDPOINT_AUTH_METHODS = ["none", "client_secret_basic", "client_secret_post"]

    def validate_token_request(self):
        super().validate_token_request()
        # 先由 Authlib 验证客户端、回调、PKCE 和 resource，避免错误请求触发他人家族吊销。
        code = self.request.authorization_code
        if code.consumed_at is not None:
            self.server.repository.record_replay(code)
            raise InvalidGrantError("Authorization code has already been consumed.")

    def save_authorization_code(self, code, request):
        self.server.repository.session.add(
            OAuthAuthorizationCode(
                code_hash=credential_hash(code),
                client_record_id=request.client.id,
                client_id=request.client.client_id,
                user_id=request.user,
                grant_id=request.grant_id,
                redirect_uri=request.payload.redirect_uri,
                scope=request.scope,
                resource=request.payload.data["resource"],
                code_challenge=request.payload.data["code_challenge"],
                code_challenge_method="S256",
                family_id=new_id(),
                issued_at=now(),
                expires_at=now() + timedelta(seconds=300),
            )
        )

    def query_authorization_code(self, code, client):
        row = self.server.repository.session.scalar(
            select(OAuthAuthorizationCode)
            .where(
                OAuthAuthorizationCode.code_hash == credential_hash(code),
                OAuthAuthorizationCode.client_record_id == client.id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            row
            and (row.consumed_at is not None or now() < aware(row.expires_at))
            and self.server.repository.grant_valid(row.grant_id, row.user_id)
        ):
            return row
        return None

    def delete_authorization_code(self, authorization_code):
        # save_token 消费代码与写新令牌在同一事务内完成。
        pass

    def authenticate_user(self, authorization_code):
        return authorization_code.user_id


class RefreshGrant(RefreshTokenGrant):
    TOKEN_ENDPOINT_AUTH_METHODS = CodeGrant.TOKEN_ENDPOINT_AUTH_METHODS
    INCLUDE_NEW_REFRESH_TOKEN = True

    def validate_token_request(self):
        super().validate_token_request()
        require_resource(self.request, self.server.settings.public_url + "/mcp", self.request.refresh_token.resource)
        if self.request.refresh_token.refresh_revoked_at is not None:
            self.server.repository.record_replay(self.request.refresh_token)
            raise InvalidGrantError("Refresh token has already been consumed.")

    def authenticate_refresh_token(self, refresh_token):
        row = self.server.repository.get_refresh_token(refresh_token)
        if row is None or row.client_record_id != self.request.client.id:
            return None
        if (
            row.refresh_expires_at
            and (row.refresh_revoked_at is not None or now() < aware(row.refresh_expires_at))
            and self.server.repository.grant_valid(row.grant_id, row.user_id)
        ):
            return row
        return None

    def authenticate_user(self, refresh_token):
        return refresh_token.user_id

    def revoke_old_credential(self, refresh_token):
        # save_token 已原子轮换。
        pass
