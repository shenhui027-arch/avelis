"""CIMD retrieval with DNS pinning; no OAuth wire protocol is implemented here."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import ssl
from urllib.parse import unquote, urlsplit

import httpcore2
from mcp.shared.auth import OAuthClientMetadata
from pydantic import ValidationError

MAX_DOCUMENT_BYTES = 5 * 1024
FETCH_TIMEOUT_SECONDS = 3.0


class CIMDError(ValueError):
    """Safe-to-report client metadata failure; map to OAuth invalid_client."""


def validate_redirect_uri(uri: object) -> str:
    """Retain exact callbacks; permit web URLs or RFC 8252 reverse-domain schemes."""
    if (
        not isinstance(uri, str)
        or not uri
        or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in uri)
        or "\\" in uri
    ):
        raise CIMDError("Invalid OAuth redirect URI")
    try:
        parts = urlsplit(uri)
        parts.port
    except ValueError as exc:
        raise CIMDError("Invalid OAuth redirect URI") from exc
    if "#" in uri or parts.username is not None or not parts.scheme:
        raise CIMDError("OAuth redirect URI must not contain userinfo or a fragment")
    if parts.scheme in ("http", "https"):
        if not parts.hostname:
            raise CIMDError("HTTP(S) OAuth redirect URI requires a host")
    elif not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?:\.[a-z0-9]+(?:-[a-z0-9]+)*)+", parts.scheme):
        raise CIMDError("Native OAuth redirect URI requires a reverse-domain scheme")
    return uri


def validate_client_metadata(document: object, client_id: str, redirect_uri: str | None = None) -> dict:
    """Use the SDK's registration model, retaining exact strings for URI binding."""
    if not isinstance(document, dict) or document.get("client_id") != client_id:
        raise CIMDError("CIMD client_id does not exactly match the document URL")
    if any(key in document for key in ("client_secret", "client_secret_expires_at")):
        raise CIMDError("CIMD must not supply a shared client secret")
    if document.get("token_endpoint_auth_method", "none") != "none":
        raise CIMDError("Only public CIMD clients are supported (none)")
    try:
        model = OAuthClientMetadata.model_validate(document)
    except ValidationError as exc:
        raise CIMDError("Invalid CIMD OAuth client metadata") from exc
    uris = document.get("redirect_uris")
    if not model.redirect_uris or not isinstance(uris, list):
        raise CIMDError("CIMD needs a nonempty redirect_uris array")
    for uri in uris:
        validate_redirect_uri(uri)
    if redirect_uri is not None and redirect_uri not in uris:
        raise CIMDError("redirect_uri does not exactly match CIMD redirect_uris")
    result = model.model_dump(mode="json", exclude_none=True)
    # Pydantic URL normalization must not broaden exact registered-URI matching.
    result.update(client_id=client_id, redirect_uris=list(uris), token_endpoint_auth_method="none")
    return result


def _target(client_id: str, local_urls: frozenset[str]) -> tuple[str, int, bool]:
    if any(ord(c) <= 32 or ord(c) >= 127 for c in client_id) or "\\" in client_id:
        raise CIMDError("CIMD URL must be an ASCII URL without whitespace")
    try:
        parts = urlsplit(client_id)
        port = parts.port
    except ValueError as exc:
        raise CIMDError("Invalid CIMD URL") from exc
    if not parts.hostname or "@" in parts.netloc or "#" in client_id:
        raise CIMDError("CIMD URL must not contain userinfo or a fragment")
    if not parts.path or any(unquote(p) in (".", "..") for p in parts.path.split("/")):
        raise CIMDError("CIMD URL must have a path without dot segments")
    fixture = (
        client_id in local_urls
        and parts.scheme in ("http", "https")
        and parts.hostname == "127.0.0.1"
        and port is not None
        and parts.netloc == f"127.0.0.1:{port}"
    )
    if not fixture and parts.scheme != "https":
        raise CIMDError("CIMD URL must use HTTPS")
    return parts.hostname, port or (80 if parts.scheme == "http" else 443), fixture


def _public_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and (
        address.ipv4_mapped or address.sixtofour or address.teredo or "%" in value
    ):
        return False
    return address.is_global and not (address.is_multicast or address.is_reserved or address.is_unspecified)


async def _resolve_public(host: str, port: int, fixture: bool) -> str:
    records = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    addresses = list(dict.fromkeys(record[4][0] for record in records))
    if not addresses or any(
        not (fixture and address == "127.0.0.1") and not _public_ip(address) for address in addresses
    ):
        raise CIMDError("CIMD DNS resolves to a forbidden nonpublic address")
    return addresses[0]


class _PinnedBackend(httpcore2.AnyIOBackend):
    """Only replace TCP destination: core retains original HTTP Host and TLS SNI."""

    def __init__(self, host: str, port: int, address: str):
        self.host, self.port, self.address = host, port, address

    async def connect_tcp(self, host: str, port: int, **kwargs):
        if host != self.host or port != self.port:
            raise CIMDError("Unexpected CIMD network destination")
        return await super().connect_tcp(self.address, port, **kwargs)


async def fetch_client_metadata(
    client_id: str,
    *,
    local_urls: frozenset[str] = frozenset(),
    max_bytes: int = MAX_DOCUMENT_BYTES,
    timeout: float = FETCH_TIMEOUT_SECONDS,
    ca_file: str | None = None,
) -> dict:
    """Fetch one bounded JSON object, refusing redirects and unsafe DNS results.

    Only a specific http(s)://127.0.0.1:PORT/path in local_urls bypasses public-IP
    requirements (and HTTPS for local HTTP). HTTPS verification stays enabled;
    ca_file can explicitly trust a test CA. The caller persists TTL-bound metadata;
    this function always retrieves and validates a new document.
    """
    if not 0 < max_bytes <= MAX_DOCUMENT_BYTES or not 0 < timeout <= FETCH_TIMEOUT_SECONDS:
        raise ValueError("CIMD limits must be positive and at most 5 KiB / 3 seconds")
    host, port, fixture = _target(client_id, local_urls)
    try:
        # Per-read timeouts alone allow a slow stream to hold a worker forever.
        async with asyncio.timeout(timeout):
            address = await _resolve_public(host, port, fixture)
            backend = _PinnedBackend(host, port, address)
            # No environment proxy, redirect handling, or secondary DNS resolution.
            async with httpcore2.AsyncConnectionPool(
                network_backend=backend, ssl_context=ssl.create_default_context(cafile=ca_file)
            ) as pool:
                async with pool.stream(
                    "GET",
                    client_id,
                    headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                    extensions={"timeout": dict.fromkeys(("connect", "read", "write", "pool"), timeout)},
                ) as response:
                    if response.status != 200:
                        raise CIMDError("CIMD fetch requires HTTP 200; redirects are refused")
                    headers = {k.lower(): v for k, v in response.headers}
                    media_type = headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower()
                    if media_type != b"application/json" and not (
                        media_type.startswith(b"application/") and media_type.endswith(b"+json")
                    ):
                        raise CIMDError("CIMD response must be JSON")
                    if headers.get(b"content-encoding", b"identity").lower() != b"identity":
                        raise CIMDError("Compressed CIMD documents are refused")
                    if int(headers.get(b"content-length", b"0")) > max_bytes:
                        raise CIMDError("CIMD response exceeds size limit")
                    body = bytearray()
                    async for chunk in response.aiter_stream():
                        if len(body) + len(chunk) > max_bytes:
                            raise CIMDError("CIMD response exceeds size limit")
                        body.extend(chunk)
                    document = json.loads(body)
    except CIMDError:
        raise
    except (
        TimeoutError,
        OSError,
        httpcore2.NetworkError,
        httpcore2.ProtocolError,
        httpcore2.TimeoutException,
        ValueError,
        RecursionError,
    ) as exc:
        raise CIMDError("CIMD fetch failed or exceeded its total timeout") from exc
    return validate_client_metadata(document, client_id)
