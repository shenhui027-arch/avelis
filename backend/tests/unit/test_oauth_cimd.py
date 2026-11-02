import asyncio
import json
import socket
import ssl
from contextlib import asynccontextmanager

import httpcore2
import pytest

from avelis.oauth import cimd
from avelis.oauth.cimd import CIMDError, fetch_client_metadata, validate_client_metadata


def metadata(client_id, **overrides):
    return {
        "client_id": client_id,
        "client_name": "PoC fixture",
        "redirect_uris": ["http://127.0.0.1:12345/callback"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        **overrides,
    }


@asynccontextmanager
async def http_fixture(responder):
    """Real loopback HTTP socket, explicitly permitted one URL at a time."""
    requests = []
    tasks = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            requests.append(request)
            status, body, headers, delay = responder(request, base)
            if delay:
                await asyncio.sleep(delay)
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n".encode()
                + headers
                + b"Connection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.remove(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        yield base, requests
    finally:
        server.close()
        await server.wait_closed()
        pending = list(tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def test_real_http_fetch_and_exact_redirect_binding():
    def respond(request, base):
        return "200 OK", json.dumps(metadata(base + "/client.json")).encode(), b"", 0

    async with http_fixture(respond) as (base, requests):
        url = base + "/client.json"
        document = await fetch_client_metadata(url, local_urls=frozenset({url}))
        validate_client_metadata(document, url, "http://127.0.0.1:12345/callback")
        with pytest.raises(CIMDError, match="redirect_uri"):
            validate_client_metadata(document, url, "http://127.0.0.1:12345/callback/")
        assert len(requests) == 1
        assert f"Host: 127.0.0.1:{base.rsplit(':', 1)[1]}".encode() in requests[0]


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/client.json",
        "file:///etc/passwd",
        "https://example.com",
        "https://example.com/client.json#",
        "https://example.com/client.json#fragment",
        "https://user:pass@example.com/client.json",
        "https://@example.com/client.json",
        "https://example.com/a/../client.json",
        "https://example.com/a/%2e/client.json",
        "https://example.com/a/%2E%2e/client.json",
        "https://example.com\\@127.0.0.1/client.json",
        "https://example.com/client.json\n",
        "https://example.com:bad/client.json",
    ],
)
async def test_invalid_url_is_rejected_before_dns(monkeypatch, url):
    async def no_dns(*args, **kwargs):
        pytest.fail("Invalid URL must not reach DNS")

    monkeypatch.setattr(cimd, "_resolve_public", no_dns)
    with pytest.raises(CIMDError):
        await fetch_client_metadata(url)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "255.255.255.255",
        "::1",
        "::",
        "fc00::1",
        "fe80::1",
        "ff02::1",
        "::ffff:127.0.0.1",
        "::ffff:8.8.8.8",
        "2002:7f00:1::1",
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",
    ],
)
async def test_all_nonpublic_or_transition_ip_results_are_denied(monkeypatch, address):
    loop = asyncio.get_running_loop()

    async def dns(*args, **kwargs):
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))]

    monkeypatch.setattr(loop, "getaddrinfo", dns)
    with pytest.raises(CIMDError, match="nonpublic"):
        await fetch_client_metadata("https://client.example/client.json")


async def test_mixed_public_and_private_dns_answers_are_denied(monkeypatch):
    loop = asyncio.get_running_loop()

    async def dns(*args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443)) for ip in ("8.8.8.8", "127.0.0.1")
        ]

    monkeypatch.setattr(loop, "getaddrinfo", dns)
    with pytest.raises(CIMDError, match="nonpublic"):
        await fetch_client_metadata("https://client.example/client.json")


async def test_dns_pinned_ip_with_original_tls_sni_host_and_certificate_verification(monkeypatch):
    url = "https://client.example:8443/client.json"
    body = json.dumps(metadata(url)).encode()
    calls, writes, tls = [], [], []
    loop = asyncio.get_running_loop()

    async def dns(*args, **kwargs):
        calls.append(("dns", args[0]))
        # A second hostname DNS lookup would get an attacker-controlled address.
        ip = "8.8.8.8" if len(calls) == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 8443))]

    class RecordingStream(httpcore2.AsyncMockStream):
        async def write(self, data, timeout=None):
            writes.append(data)

        async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            tls.append((server_hostname, ssl_context.check_hostname, ssl_context.verify_mode))
            return self

    async def tcp(self, host, port, **kwargs):
        calls.append(("tcp", host, port))
        return RecordingStream(
            [
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            ]
        )

    monkeypatch.setattr(loop, "getaddrinfo", dns)
    monkeypatch.setattr(httpcore2.AnyIOBackend, "connect_tcp", tcp)
    assert (await fetch_client_metadata(url))["client_id"] == url
    assert calls == [("dns", "client.example"), ("tcp", "8.8.8.8", 8443)]
    assert tls == [("client.example", True, ssl.CERT_REQUIRED)]
    assert b"Host: client.example:8443\r\n" in b"".join(writes)


async def test_fixture_allowlist_is_exact_url_and_http_loopback_only():
    def respond(request, base):
        return "200 OK", json.dumps(metadata(base + "/client.json")).encode(), b"", 0

    async with http_fixture(respond) as (base, requests):
        url = base + "/client.json"
        for candidate, allowed in [
            (url, frozenset()),
            (url + "?other", frozenset({url})),
            (url.replace("127.0.0.1", "localhost"), frozenset({url.replace("127.0.0.1", "localhost")})),
            (url.replace("http:", "https:"), frozenset({url})),
        ]:
            with pytest.raises(CIMDError):
                await fetch_client_metadata(candidate, local_urls=allowed)
        assert not requests


@pytest.mark.parametrize(
    "kind", ["size_header", "size_stream", "timeout", "redirect", "mismatch", "not_json", "compressed"]
)
async def test_real_http_failures_are_bounded_and_normalized(kind):
    def respond(request, base):
        body = json.dumps(metadata(base + "/client.json")).encode()
        status, headers, delay = "200 OK", b"", 0
        if kind == "size_header":
            headers, body = b"Content-Length: 99999999\r\n", b""
        elif kind == "size_stream":
            body += b" " * 1000
        elif kind == "timeout":
            delay = 1
        elif kind == "redirect":
            status, headers = "302 Found", b"Location: http://169.254.169.254/latest/meta-data/\r\n"
        elif kind == "mismatch":
            body = json.dumps(metadata(base + "/another.json")).encode()
        elif kind == "not_json":
            body = b"<html>not JSON</html>"
        elif kind == "compressed":
            headers = b"Content-Encoding: gzip\r\n"
        return status, body, headers, delay

    async with http_fixture(respond) as (base, requests):
        url = base + "/client.json"
        with pytest.raises(CIMDError):
            await fetch_client_metadata(url, local_urls=frozenset({url}), max_bytes=700, timeout=0.2)
        assert len(requests) == 1


async def test_total_timeout_also_covers_dns(monkeypatch):
    async def slow_dns(*args, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(cimd, "_resolve_public", slow_dns)
    with pytest.raises(CIMDError, match="timeout"):
        await fetch_client_metadata("https://client.example/client.json", timeout=0.01)


async def test_total_timeout_stops_a_continuously_active_slow_stream():
    finished = asyncio.Event()
    chunks = []

    async def handle(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n")
            for _ in range(100):
                writer.write(b" ")
                await writer.drain()
                chunks.append(True)
                await asyncio.sleep(0.01)
        except ConnectionError:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            finished.set()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/client.json"
    try:
        with pytest.raises(CIMDError, match="timeout"):
            await fetch_client_metadata(url, local_urls=frozenset({url}), timeout=0.2)
        assert len(chunks) > 1
        await asyncio.wait_for(finished.wait(), 1)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("size", [5 * 1024, 5 * 1024 + 1])
@pytest.mark.parametrize("content_length", [False, True])
async def test_default_five_kib_limit_checks_both_header_and_stream(size, content_length):
    def respond(request, base):
        body = json.dumps(metadata(base + "/client.json")).encode()
        body += b" " * (size - len(body))
        headers = f"Content-Length: {size}\r\n".encode() if content_length else b""
        return "200 OK", body, headers, 0

    async with http_fixture(respond) as (base, requests):
        url = base + "/client.json"
        if size <= 5 * 1024:
            document = await fetch_client_metadata(url, local_urls=frozenset({url}))
            assert document["client_id"] == url
        else:
            with pytest.raises(CIMDError, match="size limit"):
                await fetch_client_metadata(url, local_urls=frozenset({url}))
        assert len(requests) == 1


@pytest.mark.parametrize("kwargs", [{"max_bytes": 0}, {"max_bytes": 5121}, {"timeout": 0}, {"timeout": 3.1}])
async def test_fetch_limits_cannot_expand_production_bounds(monkeypatch, kwargs):
    async def no_dns(*args, **kwargs):
        pytest.fail("Invalid limits must not reach DNS")

    monkeypatch.setattr(cimd, "_resolve_public", no_dns)
    with pytest.raises(ValueError, match="5 KiB / 3 seconds"):
        await fetch_client_metadata("https://client.example/client.json", **kwargs)


async def test_environment_proxy_is_never_used(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")

    def respond(request, base):
        return "200 OK", json.dumps(metadata(base + "/client.json")).encode(), b"", 0

    async with http_fixture(respond) as (base, requests):
        url = base + "/client.json"
        assert (await fetch_client_metadata(url, local_urls=frozenset({url})))["client_id"] == url
        assert len(requests) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"redirect_uris": []},
        {"redirect_uris": ["not a URL"]},
        {"redirect_uris": ["https://client.example/cb#fragment"]},
        {"redirect_uris": ["https://user:pass@client.example/cb"]},
        {"token_endpoint_auth_method": "client_secret_post"},
        {"token_endpoint_auth_method": "private_key_jwt"},
        {"client_secret": "must-not-be-accepted"},
        {"client_secret_expires_at": 0},
    ],
)
def test_metadata_uses_sdk_validation_and_restricts_public_client_profile(overrides):
    url = "https://client.example/client.json"
    with pytest.raises(CIMDError):
        validate_client_metadata(metadata(url, **overrides), url)


def test_redirect_binding_preserves_original_string_instead_of_url_normalization():
    url = "https://client.example/client.json"
    document = validate_client_metadata(metadata(url, redirect_uris=["https://client.example"]), url)
    assert document["redirect_uris"] == ["https://client.example"]
    with pytest.raises(CIMDError):
        validate_client_metadata(document, url, "https://client.example/")


@pytest.mark.parametrize(
    "uri",
    [
        "javascript://client.example/alert(1)",
        "data://client.example/text/html,payload",
        "vbscript://client.example/payload",
        "file://client.example/callback",
        "blob://client.example/callback",
        "about://client.example/callback",
        "filesystem://client.example/callback",
        "chrome://settings/callback",
        "edge://settings/callback",
        "resource://client.example/callback",
        "custom-app:/callback",
        "https://client.example/callback\n",
        "https://client.example/callback\u2003",
        "https://client.example\\@other.example/callback",
        "https://client.example:bad/callback",
        "com.example.app:/callback#fragment",
        "com.example.app://user@client.example/callback",
        "com..example:/callback",
    ],
)
def test_metadata_rejects_unsafe_browser_schemes_and_ambiguous_redirects(uri):
    url = "https://client.example/client.json"
    with pytest.raises(CIMDError):
        validate_client_metadata(metadata(url, redirect_uris=[uri]), url)


@pytest.mark.parametrize(
    "uri", ["https://client.example/callback", "http://127.0.0.1:23456/callback", "com.example.app:/oauth2redirect"]
)
def test_redirect_profile_preserves_web_and_reverse_domain_native_callbacks(uri):
    url = "https://client.example/client.json"
    result = validate_client_metadata(metadata(url, redirect_uris=[uri]), url, uri)
    assert result["redirect_uris"] == [uri]
