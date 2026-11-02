"""模块开关、SPA 回退和真实 SDK 子应用挂载边界。"""

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from avelis.api import app as api_app
from avelis.config import build_settings

PROTOCOL_ENDPOINTS = [
    ("GET", "/.well-known/oauth-authorization-server"),
    ("GET", "/.well-known/oauth-protected-resource/mcp"),
    ("GET", "/oauth/authorize"),
    ("POST", "/oauth/token"),
    ("POST", "/oauth/revoke"),
    ("POST", "/oauth/register"),
    ("POST", "/mcp"),
    ("GET", "/mcp"),
    ("GET", "/mcp/"),
    ("GET", "/api/oauth/consent/example"),
    ("POST", "/api/oauth/consent/example/approve"),
    ("POST", "/api/oauth/consent/example/deny"),
    ("GET", "/api/oauth/authorizations"),
    ("DELETE", "/api/oauth/authorizations/example"),
    ("GET", "/api/oauth/clients"),
    ("POST", "/api/oauth/clients/example/revoke"),
]


@pytest.mark.parametrize("with_frontend", [False, True])
def test_disabled_endpoints_are_404_and_not_in_schema(monkeypatch, tmp_path, with_frontend):
    settings = build_settings({"AVELIS_SECRET": "test-secret-mcp-x"})
    if with_frontend:
        (tmp_path / "index.html").write_text("<html>Avelis</html>", encoding="utf-8")
        settings = replace(settings, frontend_dir=tmp_path)
    monkeypatch.setattr(api_app, "get_settings", lambda: settings)
    application = api_app.create_app()
    assert all("/oauth/" not in path for path in application.openapi()["paths"])
    with TestClient(application) as client:
        for method, path in PROTOCOL_ENDPOINTS:
            response = client.request(method, path, follow_redirects=False)
            assert response.status_code == 404, (method, path, response.status_code)
        assert client.get("/api/health").json() == {"status": "ok"}
        if with_frontend:
            assert client.get("/account").status_code == 200


def test_enabled_metadata_and_mcp_have_no_trailing_slash_redirect(monkeypatch, tmp_path):
    (tmp_path / "index.html").write_text("<html>Avelis</html>", encoding="utf-8")
    settings = build_settings(
        {
            "AVELIS_SECRET": "test-secret-mcp-x",
            "AVELIS_MCP_ENABLED": "true",
            "AVELIS_PUBLIC_URL": "https://avelis.example",
        }
    )
    monkeypatch.setattr(api_app, "get_settings", lambda: replace(settings, frontend_dir=tmp_path))
    application = api_app.create_app()
    assert "/api/oauth/consent/{ticket}" in application.openapi()["paths"]
    with TestClient(application, base_url=settings.public_url) as client:
        assert client.get("/.well-known/oauth-authorization-server").json()["issuer"] == settings.public_url
        resource = client.get("/.well-known/oauth-protected-resource/mcp")
        assert resource.json()["resource"] == settings.public_url + "/mcp"
        response = client.post("/mcp", json={}, follow_redirects=False)
        assert response.status_code == 401
        assert "resource_metadata=" in response.headers["www-authenticate"]
        assert "location" not in response.headers
        assert client.get("/oauth/consent?ticket=example").status_code == 200


def test_offline_openapi_exports_optional_contract_without_enabling_runtime(monkeypatch, tmp_path):
    from avelis import bootstrap

    settings = build_settings({"AVELIS_SECRET": "test-secret-mcp-x"})
    monkeypatch.setattr(bootstrap, "get_settings", lambda: settings)
    monkeypatch.setattr(api_app, "get_settings", lambda: settings)
    target = tmp_path / "openapi.json"
    bootstrap.main(["openapi", str(target)])
    document = json.loads(target.read_text(encoding="utf-8"))
    assert "/api/oauth/consent/{ticket}" in document["paths"]
    assert "/api/oauth/clients/{client_record_id}/revoke" in document["paths"]
    assert not settings.mcp_enabled
    application = api_app.create_app()
    assert "/api/oauth/consent/{ticket}" not in application.openapi()["paths"]
    with TestClient(application) as client:
        assert client.get("/api/oauth/authorizations").status_code == 404
