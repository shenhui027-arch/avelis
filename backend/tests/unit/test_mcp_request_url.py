"""请求地址只使用 ASGI scheme、合法 Host 与 root_path，代理信任交给 uvicorn。"""

import pytest
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from avelis.api.app import create_app
from avelis.config import build_settings
from tests.unit.test_oauth_server import (
    BASE,
    issue_ticket,
    oauth_api,  # noqa: F401
)


@pytest.mark.parametrize("peer,scheme", [("127.0.0.1", "https"), ("192.0.2.10", "http")])
def test_proxy_scheme_obeys_uvicorn_trust_and_forwarded_host_is_ignored(peer, scheme):
    settings = build_settings({"AVELIS_SECRET": "test-secret-admin-y", "AVELIS_FORWARDED_ALLOW_IPS": "127.0.0.1"})
    app = ProxyHeadersMiddleware(create_app(settings=settings), trusted_hosts=settings.forwarded_allow_ips)
    with TestClient(app, base_url="http://avelis.test:5173", client=(peer, 43210), root_path="/avelis") as client:
        headers = {
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": "198.51.100.10",
            "X-Forwarded-Host": "attacker.example",
            "X-Forwarded-Prefix": "/attacker",
            "Forwarded": "host=attacker.example;proto=https",
        }
        issuer = scheme + "://avelis.test:5173/avelis"
        metadata = client.get("/avelis/.well-known/oauth-authorization-server", headers=headers)
        assert metadata.status_code == 200, metadata.text
        assert metadata.json()["issuer"] == issuer
        prm = client.get("/avelis/.well-known/oauth-protected-resource/mcp", headers=headers)
        assert prm.json()["resource"] == issuer + "/mcp"
        assert prm.json()["authorization_servers"] == [issuer]
        challenge = client.post("/avelis/mcp", headers=headers, json={})
        assert challenge.status_code == 401
        assert (
            f'resource_metadata="{issuer}/.well-known/oauth-protected-resource/mcp"'
            in challenge.headers["www-authenticate"]
        )


@pytest.mark.parametrize("host,issuer", [("[::1]:8080", "http://[::1]:8080"), ("AVELIS.test:80", "http://avelis.test")])
def test_host_normalization_is_shared_by_discovery_prm_and_challenge(host, issuer):
    settings = build_settings({"AVELIS_SECRET": "test-secret-admin-y"})
    with TestClient(create_app(settings=settings)) as client:
        headers = {"Host": host}
        metadata = client.get("/.well-known/oauth-authorization-server", headers=headers)
        assert metadata.status_code == 200, metadata.text
        assert metadata.json()["issuer"] == issuer
        prm = client.get("/.well-known/oauth-protected-resource/mcp", headers=headers)
        assert prm.json()["resource"] == issuer + "/mcp"
        assert prm.json()["authorization_servers"] == [issuer]
        challenge = client.post("/mcp", json={}, headers=headers)
        assert challenge.status_code == 401
        assert (
            f'resource_metadata="{issuer}/.well-known/oauth-protected-resource/mcp"'
            in challenge.headers["www-authenticate"]
        )


def test_multiple_host_headers_are_rejected_before_discovery():
    settings = build_settings({"AVELIS_SECRET": "test-secret-admin-y"})
    with TestClient(create_app(settings=settings)) as client:
        response = client.get(
            "/.well-known/oauth-authorization-server", headers=[("Host", "avelis.test"), ("Host", "other.test")]
        )
        assert response.status_code == 400
        assert "issuer" not in response.json()


def test_explicit_public_url_keeps_existing_startup_transport_validation():
    settings = build_settings({"AVELIS_SECRET": "test-secret-admin-y", "AVELIS_PUBLIC_URL": "http://avelis.test:8080"})
    with pytest.raises(ValueError, match="https"):
        create_app(settings=settings)


def test_restored_consent_keeps_configured_issuer_over_internal_http(oauth_api):
    from urllib.parse import parse_qs, urlsplit

    ticket = issue_ticket(oauth_api)
    with TestClient(oauth_api.app, base_url="http://internal.avelis.test") as client:
        path = f"/api/oauth/consent/{ticket}"
        assert client.get(path).status_code == 200
        approved = client.post(path + "/approve")
        assert approved.status_code == 200, approved.text
        assert parse_qs(urlsplit(approved.json()["redirect_to"]).query)["iss"] == [BASE]


@pytest.mark.parametrize("host", ["09", "a.1", "a.123", "99999999999999999999999999999", "0x7f.1", "2130706433"])
@pytest.mark.parametrize(
    "path", ["/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource/mcp"]
)
def test_url_normalization_errors_are_bad_requests(host, path):
    settings = build_settings({"AVELIS_SECRET": "test-secret-admin-y"})
    with TestClient(create_app(settings=settings)) as client:
        response = client.get(path, headers={"Host": host})
        assert response.status_code == 400
        assert "issuer" not in response.json() and "resource" not in response.json()


def test_inferred_mcp_still_rejects_cross_origin_requests():
    settings = build_settings({"AVELIS_SECRET": "test-secret-admin-y"})
    with TestClient(create_app(settings=settings), base_url="http://avelis.test:8080") as client:
        response = client.post("/mcp", json={}, headers={"Origin": "http://other.test:8080"})
        assert response.status_code == 403


@pytest.mark.parametrize("with_frontend", [False, True])
@pytest.mark.parametrize("prefix", ["/m", "/mc"])
def test_root_path_partial_segment_cannot_bypass_mcp_authentication(tmp_path, prefix, with_frontend):
    from dataclasses import replace

    from tests.unit.test_mcp_resource import modern_message

    settings = build_settings({"AVELIS_SECRET": "test-secret-admin-y"})
    if with_frontend:
        (tmp_path / "index.html").write_text("<html>Avelis</html>", encoding="utf-8")
        settings = replace(settings, frontend_dir=tmp_path)
    with TestClient(create_app(settings=settings), root_path=prefix) as client:
        for path in ("/mcp", prefix + "/mcp"):
            response = client.post(
                path,
                json=modern_message(),
                headers={
                    "MCP-Protocol-Version": "2026-07-28",
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert response.status_code == 401, response.text
