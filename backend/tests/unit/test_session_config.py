"""登录凭证有效期与最长会话时长的配置校验。"""

from __future__ import annotations

import pytest

from avelis.config import Settings, build_settings

SECRET = "offline-unit-test-secret-0123456789"


def test_session_settings_defaults():
    settings = build_settings({"AVELIS_SECRET": SECRET})
    assert settings.access_token_minutes == Settings.access_token_minutes == 120
    assert settings.session_max_hours == Settings.session_max_hours == 168


def test_session_settings_load_environment(monkeypatch):
    monkeypatch.setenv("AVELIS_ENV_FILE", "/nonexistent")
    monkeypatch.setenv("AVELIS_SECRET", SECRET)
    monkeypatch.setenv("AVELIS_ACCESS_TOKEN_MINUTES", "45")
    monkeypatch.setenv("AVELIS_SESSION_MAX_HOURS", "24")
    settings = build_settings()
    assert settings.access_token_minutes == 45
    assert settings.session_max_hours == 24


@pytest.mark.parametrize("key", ["AVELIS_ACCESS_TOKEN_MINUTES", "AVELIS_SESSION_MAX_HOURS"])
@pytest.mark.parametrize("value", ["0", "-1", "1.5", "abc", ""])
def test_session_settings_reject_invalid_values(key, value):
    with pytest.raises(ValueError, match=f"{key} 必须为正整数"):
        build_settings({"AVELIS_SECRET": SECRET, key: value})
