"""Bound anonymous OAuth inputs before parsing protocol metadata or persisting it."""

from authlib.oauth2.rfc7591.errors import InvalidClientMetadataError, InvalidRedirectURIError

REGISTRATION_BYTES = 16 * 1024
MAX_REDIRECT_URIS = 5
MAX_URI_LENGTH = 512
AUTHORIZATION_BYTES = 8192


def validate_metadata_budget(data: dict) -> None:
    for key, value in data.items():
        if key == "redirect_uris":
            if not isinstance(value, list) or any(not isinstance(uri, str) or not uri for uri in value):
                raise InvalidClientMetadataError("redirect_uris must be an array of nonempty strings.")
            if not 1 <= len(value) <= MAX_REDIRECT_URIS or any(len(uri) > MAX_URI_LENGTH for uri in value):
                raise InvalidRedirectURIError("redirect_uris exceeds the callback budget.")
            continue
        string_limit = {"client_id": 2048, "client_name": 256, "scope": 256, "token_endpoint_auth_method": 32}.get(
            key, MAX_URI_LENGTH
        )
        if isinstance(value, str) and len(value) > string_limit:
            raise InvalidClientMetadataError(f"'{key}' exceeds its length budget.")
        if isinstance(value, list) and (
            len(value) > 5 or any(not isinstance(item, str) or len(item) > 256 for item in value)
        ):
            raise InvalidClientMetadataError(f"'{key}' exceeds its array budget.")
        if key == "scope" and (not isinstance(value, str) or len(value.split()) > 16):
            raise InvalidClientMetadataError("scope exceeds its budget.")


def authorization_within_budget(request) -> bool:
    if len(request.scope.get("query_string", b"")) > AUTHORIZATION_BYTES:
        return False
    pairs = request.query_params.multi_items()
    if len(pairs) > 32:
        return False
    limits = {"client_id": 2048, "resource": 2048, "scope": 256, "code_challenge": 128}
    return all(len(key) <= 64 and len(value) <= limits.get(key, MAX_URI_LENGTH) for key, value in pairs)
