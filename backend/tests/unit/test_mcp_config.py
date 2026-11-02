"""MCP 配置与精确 issuer/resource 身份。"""

import pytest

from avelis.config import ConfigError, build_settings
from avelis.configfile import CONFIG_KEYS, flatten, read_config

SECRET = "test-secret-mcp-x"


def settings(**values):
    return build_settings({"AVELIS_SECRET": SECRET, **values})


def test_mcp_defaults_are_disabled_and_short_lived():
    value = settings()
    assert not value.mcp_enabled
    assert value.mcp_access_token_seconds == 900
    assert value.mcp_refresh_token_seconds == 2592000
    assert value.mcp_cimd_cache_ttl_seconds == 3600
    assert value.mcp_cimd_allowed_urls == ()


@pytest.mark.parametrize("enabled", ["true", "false"])
@pytest.mark.parametrize("suffix", ["", "/", "///", "/avelis/", "/avelis///"])
def test_public_url_is_normalized_once(enabled, suffix):
    value = settings(AVELIS_MCP_ENABLED=enabled, AVELIS_PUBLIC_URL="https://avelis.example" + suffix)
    expected = "https://avelis.example" + suffix.rstrip("/")
    assert value.public_url == expected
    assert value.mcp_resource_url == expected + "/mcp"
    if enabled == "false":
        return
    assert "https://avelis.example" in value.cors_origins
    assert "https://avelis.example/" not in value.cors_origins


@pytest.mark.parametrize("enabled", ["true", "false"])
@pytest.mark.parametrize("suffix", ["?a=1", "#fragment", "?", "#", "/avelis/?a=1", "/avelis/#fragment"])
def test_public_url_rejects_query_and_fragment_at_startup(enabled, suffix):
    with pytest.raises(ConfigError, match="AVELIS_PUBLIC_URL"):
        settings(AVELIS_MCP_ENABLED=enabled, AVELIS_PUBLIC_URL="https://avelis.example" + suffix)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "relative",
        "ftp://avelis.example",
        "https://user:pass@avelis.example",
        "https://avelis.example?a=1",
        "https://avelis.example#fragment",
        "https://avelis.example:bad",
        "https://avelis.example/has space",
    ],
)
def test_enabled_mcp_requires_valid_public_url(url):
    with pytest.raises(ConfigError, match="AVELIS_PUBLIC_URL"):
        settings(AVELIS_MCP_ENABLED="true", AVELIS_PUBLIC_URL=url)


@pytest.mark.parametrize(
    "key", ["AVELIS_MCP_ACCESS_TOKEN_SECONDS", "AVELIS_MCP_REFRESH_TOKEN_SECONDS", "AVELIS_MCP_CIMD_CACHE_TTL_SECONDS"]
)
@pytest.mark.parametrize("value", ["0", "-1", "invalid"])
def test_mcp_lifetimes_require_positive_integers(key, value):
    with pytest.raises(ConfigError, match=key):
        settings(**{key: value})


def test_mcp_toml_and_environment_share_configuration_mapping(tmp_path):
    path = tmp_path / "avelis.toml"
    path.write_text(
        '[server]\npublic_url="https://avelis.example"\n[mcp]\nenabled=true\naccess_token_seconds=120\n'
        "refresh_token_seconds=3600\ncimd_cache_ttl_seconds=60\n"
        'cimd_allowed_urls=["http://127.0.0.1:9876/client.json"]\n',
        encoding="utf-8",
    )
    values = read_config({"AVELIS_CONFIG": str(path)})
    values["AVELIS_SECRET"] = SECRET
    value = build_settings(values)
    assert value.mcp_enabled and value.mcp_access_token_seconds == 120
    assert value.mcp_refresh_token_seconds == 3600 and value.mcp_cimd_cache_ttl_seconds == 60
    assert value.mcp_cimd_allowed_urls == ("http://127.0.0.1:9876/client.json",)
    assert flatten(CONFIG_KEYS["mcp.cimd_allowed_urls"].default) == ""
