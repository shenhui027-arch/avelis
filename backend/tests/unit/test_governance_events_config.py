"""Webhook 配置走统一 TOML/环境变量注册与汇总校验。"""

import pytest

from avelis.config import ConfigError, build_settings
from avelis.configfile import read_config

SECRET = "offline-unit-test-secret-0123456789"


def test_webhook_defaults_and_environment_override():
    settings = build_settings({"AVELIS_SECRET": SECRET})
    assert settings.webhook_allow_private_urls is False
    assert settings.webhook_concurrency == 4
    assert settings.webhook_max_attempts == 8
    assert settings.webhook_retry_max_minutes == 60
    settings = build_settings(
        {
            "AVELIS_SECRET": SECRET,
            "AVELIS_WEBHOOK_ALLOW_PRIVATE_URLS": "true",
            "AVELIS_WEBHOOK_CONCURRENCY": "1",
            "AVELIS_WEBHOOK_MAX_ATTEMPTS": "3",
            "AVELIS_WEBHOOK_RETRY_MAX_MINUTES": "10",
        }
    )
    assert settings.webhook_allow_private_urls is True
    assert settings.webhook_concurrency == 1
    assert settings.webhook_max_attempts == 3
    assert settings.webhook_retry_max_minutes == 10


@pytest.mark.parametrize(
    "key", ["AVELIS_WEBHOOK_MAX_ATTEMPTS", "AVELIS_WEBHOOK_RETRY_MAX_MINUTES", "AVELIS_WEBHOOK_CONCURRENCY"]
)
@pytest.mark.parametrize("value", ["0", "-1", "invalid"])
def test_webhook_retry_limits_are_positive(key, value):
    with pytest.raises(ConfigError, match=key):
        build_settings({"AVELIS_SECRET": SECRET, key: value})


def test_webhook_concurrency_has_a_bounded_resource_limit():
    assert build_settings({"AVELIS_SECRET": SECRET, "AVELIS_WEBHOOK_CONCURRENCY": "64"}).webhook_concurrency == 64
    with pytest.raises(ConfigError, match="AVELIS_WEBHOOK_CONCURRENCY"):
        build_settings({"AVELIS_SECRET": SECRET, "AVELIS_WEBHOOK_CONCURRENCY": "65"})


def test_webhook_allow_private_requires_boolean():
    with pytest.raises(ConfigError, match="AVELIS_WEBHOOK_ALLOW_PRIVATE_URLS"):
        build_settings({"AVELIS_SECRET": SECRET, "AVELIS_WEBHOOK_ALLOW_PRIVATE_URLS": "maybe"})


def test_webhook_toml_configuration(tmp_path):
    path = tmp_path / "avelis.toml"
    path.write_text(
        f'[security]\nsecret = "{SECRET}"\n[webhook]\nallow_private_urls = true\n'
        "max_attempts = 4\nretry_max_minutes = 7\nconcurrency = 2\n",
        encoding="utf-8",
    )
    settings = build_settings(read_config({"AVELIS_CONFIG": str(path)}))
    assert (settings.webhook_allow_private_urls, settings.webhook_max_attempts, settings.webhook_retry_max_minutes) == (
        True,
        4,
        7,
    )
    assert settings.webhook_concurrency == 2
