"""按发现文档实际请求端点，验证规范化后的 OAuth/MCP 身份与资源绑定。"""

import asyncio
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from avelis import db
from avelis.api.app import create_app
from avelis.config import build_settings
from avelis.mcp.auth import StoredTokenVerifier
from tests.unit.test_mcp_resource import modern_message
from tests.unit.test_oauth_server import (
    BASE,
    authorization_params,
    exchange_params,
    oauth_api,  # noqa: F401
)


@pytest.mark.parametrize(
    "public_url,origin,prefix",
    [
        *[(BASE + suffix, BASE, suffix.rstrip("/")) for suffix in ("", "/", "///", "/avelis/", "/avelis///")],
        *[
            (None, origin, prefix)
            for origin in ("http://avelis.test:8080", "http://127.0.0.1:8080")
            for prefix in ("", "/avelis")
        ],
    ],
)
def test_discovered_urls_route_and_share_one_identity(oauth_api, monkeypatch, tmp_path, public_url, origin, prefix):
    environment = {"AVELIS_SECRET": "test-secret-mcp-x"}
    if public_url is not None:
        environment["AVELIS_PUBLIC_URL"] = public_url
    settings = build_settings(environment)
    issuer = origin + prefix
    resource = issuer + "/mcp"
    (tmp_path / "index.html").write_text("<html>Consent</html>", encoding="utf-8")
    application = create_app(settings=replace(settings, frontend_dir=tmp_path))
    application.dependency_overrides.update(oauth_api.app.dependency_overrides)
    maker = sessionmaker(bind=oauth_api.session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(db, "session_factory", lambda: maker)

    with TestClient(application, base_url=origin, root_path=prefix, follow_redirects=False) as client:
        discovery = client.get(prefix + "/.well-known/oauth-authorization-server")
        assert discovery.status_code == 200
        metadata = discovery.json()
        assert metadata["issuer"] == issuer
        for key, path in (
            ("authorization_endpoint", "/oauth/authorize"),
            ("token_endpoint", "/oauth/token"),
            ("registration_endpoint", "/oauth/register"),
            ("revocation_endpoint", "/oauth/revoke"),
        ):
            assert metadata[key] == issuer + path

        # SDK 的 challenge 提供 RFC 9728 地址；从该文档取 resource 与 issuer。
        challenge = client.post(urlsplit(resource).path, json={})
        assert challenge.status_code == 401
        metadata_url = challenge.headers["www-authenticate"].split('resource_metadata="')[1].split('"')[0]
        expected_metadata_url = (
            origin + "/.well-known/oauth-protected-resource" + prefix + "/mcp"
            if public_url is not None
            else issuer + "/.well-known/oauth-protected-resource/mcp"
        )
        assert metadata_url == expected_metadata_url
        protected = client.get(urlsplit(metadata_url).path)
        assert protected.status_code == 200
        assert protected.json()["resource"] == resource
        assert protected.json()["authorization_servers"] == [metadata["issuer"]]

        registered = client.post(
            urlsplit(metadata["registration_endpoint"]).path,
            json={
                "redirect_uris": ["http://127.0.0.1:9876/callback"],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
            },
        )
        assert registered.status_code == 201, registered.text
        client_id = registered.json()["client_id"]
        authorization = client.get(
            urlsplit(metadata["authorization_endpoint"]).path,
            params=authorization_params(client_id=client_id, resource=resource),
        )
        assert authorization.status_code == 302, authorization.text
        consent_url = authorization.headers["location"]
        assert consent_url.startswith(issuer + "/oauth/consent?")
        assert client.get(urlsplit(consent_url).path).text == "<html>Consent</html>"
        ticket = parse_qs(urlsplit(consent_url).query)["ticket"][0]
        assert client.get(prefix + f"/api/oauth/consent/{ticket}").status_code == 200
        approved = client.post(prefix + f"/api/oauth/consent/{ticket}/approve")
        assert approved.status_code == 200, approved.text
        callback = parse_qs(urlsplit(approved.json()["redirect_to"]).query)
        assert callback["iss"] == protected.json()["authorization_servers"] == [metadata["issuer"]]
        token_path = urlsplit(metadata["token_endpoint"]).path
        exchange = exchange_params(callback["code"][0], client_id=client_id, resource=resource)
        # 错误的双斜杠资源不能兑换代码；正确资源仍可兑换同一个代码。
        rejected = client.post(token_path, data={**exchange, "resource": issuer + "//mcp"})
        assert rejected.status_code == 400 and rejected.json()["error"] == "invalid_target"
        if public_url is None:
            other_origin = "http://other.avelis.test:8080"
            rejected = client.post(
                other_origin + token_path,
                data={**exchange, "resource": other_origin + prefix + "/mcp"},
            )
            assert rejected.status_code == 400 and rejected.json()["error"] == "invalid_target"
        token_response = client.post(token_path, data=exchange)
        assert token_response.status_code == 200, token_response.text
        tokens = token_response.json()
        assert tokens["resource"] == resource
        stored = oauth_api.repository.verify_access_token(tokens["access_token"], resource)
        assert stored is not None and stored.resource == resource
        if public_url is not None:
            verified = asyncio.run(StoredTokenVerifier(maker, settings).verify_token(tokens["access_token"]))
            assert verified.resource == resource
            assert verified.claims["iss"] == metadata["issuer"]
        headers = {
            "Authorization": "Bearer " + tokens["access_token"],
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "tools/list",
            "Accept": "application/json, text/event-stream",
        }
        mcp = client.post(
            urlsplit(protected.json()["resource"]).path,
            headers=headers,
            json=modern_message(),
        )
        assert mcp.status_code == 200, mcp.text
        if public_url is None:
            rejected = client.post(other_origin + prefix + "/mcp", headers=headers, json=modern_message())
            assert rejected.status_code == 401, rejected.text
            assert (
                f'resource_metadata="{other_origin}{prefix}/.well-known/oauth-protected-resource/mcp"'
                in rejected.headers["www-authenticate"]
            )
        refresh = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": tokens["refresh_token"],
            "resource": resource,
        }
        rejected = client.post(token_path, data={**refresh, "resource": issuer + "//mcp"})
        assert rejected.status_code == 400 and rejected.json()["error"] == "invalid_target"
        if public_url is None:
            rejected = client.post(
                other_origin + token_path,
                data={**refresh, "resource": other_origin + prefix + "/mcp"},
            )
            assert rejected.status_code == 400 and rejected.json()["error"] == "invalid_target"
        refreshed = client.post(token_path, data=refresh)
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["resource"] == resource
        revoked = client.post(
            urlsplit(metadata["revocation_endpoint"]).path,
            data={"client_id": client_id, "token": refreshed.json()["access_token"]},
        )
        assert revoked.status_code == 200, revoked.text
        rejected = client.post(
            urlsplit(resource).path,
            headers={**headers, "Authorization": "Bearer " + refreshed.json()["access_token"]},
            json=modern_message(),
        )
        assert rejected.status_code == 401, rejected.text
