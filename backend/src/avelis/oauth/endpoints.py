"""RFC 7009、7591、8414 端点策略。"""

from __future__ import annotations

from authlib.oauth2.rfc7009 import RevocationEndpoint
from authlib.oauth2.rfc7591 import ClientRegistrationEndpoint
from authlib.oauth2.rfc7591.errors import InvalidClientMetadataError
from authlib.oauth2.rfc8414 import AuthorizationServerMetadata

from avelis import audit
from avelis.models import OAuthClient, User

from .cimd import CIMDError, validate_redirect_uri
from .grants import CodeGrant
from .repository import SCOPES, credential_hash


class RevokeEndpoint(RevocationEndpoint):
    CLIENT_AUTH_METHODS = CodeGrant.TOKEN_ENDPOINT_AUTH_METHODS

    def query_token(self, token_string, token_type_hint):
        return self.server.repository.get_access_token(token_string) or self.server.repository.get_refresh_token(
            token_string
        )

    def revoke_token(self, token, request):
        self.server.repository.revoke_family(token)
        audit.record(
            self.server.repository.session,
            self.server.repository.session.get(User, token.user_id),
            "oauth.revoke",
            "oauth_grant",
            token.grant_id,
            client_id=token.client_id,
        )


class RegistrationEndpoint(ClientRegistrationEndpoint):
    def authenticate_token(self, request):
        return True

    def get_server_metadata(self):
        return self.server.metadata

    def extract_client_metadata(self, request):
        data = request.payload.data
        for key in ("redirect_uris", "grant_types", "response_types"):
            if key in data and (
                not isinstance(data[key], list) or not all(isinstance(x, str) and x for x in data[key])
            ):
                raise InvalidClientMetadataError(f"'{key}' must be an array of strings.")
        for key in ("scope", "token_endpoint_auth_method", "client_name"):
            if key in data and not isinstance(data[key], str):
                raise InvalidClientMetadataError(f"'{key}' must be a string.")
        metadata = super().extract_client_metadata(request)
        if not metadata.get("redirect_uris"):
            raise InvalidClientMetadataError("At least one redirect URI is required.")
        for redirect in metadata["redirect_uris"]:
            try:
                validate_redirect_uri(redirect)
            except CIMDError:
                raise InvalidClientMetadataError("Invalid redirect URI.") from None
        metadata.setdefault("scope", " ".join(SCOPES))
        metadata.setdefault("grant_types", ["authorization_code"])
        metadata.setdefault("response_types", ["code"])
        return metadata

    def generate_client_secret(self, request):
        if request.payload.data.get("token_endpoint_auth_method") == "none":
            return None
        return super().generate_client_secret(request)

    def save_client(self, client_info, client_metadata, request):
        secret = client_info.get("client_secret")
        row = OAuthClient(
            client_id=client_info["client_id"],
            source="dynamic",
            redirect_uris=client_metadata["redirect_uris"],
            scope=client_metadata["scope"],
            grant_types=client_metadata["grant_types"],
            response_types=client_metadata["response_types"],
            token_endpoint_auth_method=client_metadata["token_endpoint_auth_method"],
            secret_hash=credential_hash(secret) if secret else None,
            client_name=client_metadata.get("client_name", "OAuth client"),
        )
        self.server.repository.session.add(row)
        self.server.repository.session.flush()
        audit.record(
            self.server.repository.session,
            None,
            "oauth.client_register",
            "oauth_client",
            row.id,
            client_id=row.client_id,
            source="dynamic",
        )
        return row


def authorization_metadata(settings):
    base = settings.public_url
    metadata = AuthorizationServerMetadata(
        issuer=base,
        authorization_endpoint=f"{base}/oauth/authorize",
        token_endpoint=f"{base}/oauth/token",
        registration_endpoint=f"{base}/oauth/register",
        revocation_endpoint=f"{base}/oauth/revoke",
        scopes_supported=list(SCOPES),
        response_types_supported=["code"],
        response_modes_supported=["query"],
        grant_types_supported=["authorization_code", "refresh_token"],
        token_endpoint_auth_methods_supported=CodeGrant.TOKEN_ENDPOINT_AUTH_METHODS,
        revocation_endpoint_auth_methods_supported=CodeGrant.TOKEN_ENDPOINT_AUTH_METHODS,
        code_challenge_methods_supported=["S256"],
        authorization_response_iss_parameter_supported=True,
        client_id_metadata_document_supported=True,
    )
    metadata.validate()
    return metadata
