"""部署配置的离线解析与默认值。"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from avelis.config import REPO_ROOT, build_settings

SECRET = "offline-unit-test-secret-0123456789"


def settings(**env):
    return build_settings({"AVELIS_SECRET": SECRET, **env})


@pytest.mark.parametrize(("field", "default"), [
    ("query_timeout_seconds", 30),
    ("query_mem_limit_bytes", 2 * 1024**3),
    ("query_max_rows", 1000),
    ("answer_cache_seconds", 300),
    ("collect_interval_minutes", 15),
    ("host", "127.0.0.1"),
    ("port", 8000),
    ("workers", 1),
    ("forwarded_allow_ips", "127.0.0.1"),
    ("log_level", "info"),
    ("frontend_dir", None),
    ("auto_migrate", False),
    ("auto_init_admin", False),
    ("db_pool_size", 5),
    ("db_max_overflow", 10),
    ("db_pool_recycle_seconds", 1800),
    ("import_max_bytes", 2 * 1024**2),
    ("ask_max_tool_rounds", 8),
    ("analysis_tool_calls", 30),
    ("analysis_seconds", 300),
    ("analysis_explore_limit", 8),
    ("analysis_model_requests", 16),
    ("analysis_input_tokens", 552321),
    ("analysis_output_tokens", 30716),
    ("model_timeout_seconds", 60),
])
def test_deploy_defaults_match_existing_behavior(field, default):
    assert getattr(settings(), field) == default


POSITIVE_FIELDS = (
    "QUERY_TIMEOUT_SECONDS", "QUERY_MEM_LIMIT_BYTES", "QUERY_MAX_ROWS", "ANSWER_CACHE_SECONDS",
    "WORKERS", "IMPORT_MAX_BYTES", "ASK_MAX_TOOL_ROUNDS", "ANALYSIS_TOOL_CALLS", "ANALYSIS_SECONDS",
    "ANALYSIS_EXPLORE_LIMIT", "ANALYSIS_MODEL_REQUESTS", "MODEL_TIMEOUT_SECONDS",
)
NONNEGATIVE_FIELDS = (
    "DB_POOL_SIZE", "DB_MAX_OVERFLOW", "DB_POOL_RECYCLE_SECONDS", "ANALYSIS_INPUT_TOKENS", "ANALYSIS_OUTPUT_TOKENS",
)


@pytest.mark.parametrize("name", POSITIVE_FIELDS + NONNEGATIVE_FIELDS + ("PORT", "COLLECT_INTERVAL_MINUTES"))
def test_numeric_environment_overrides(name):
    assert getattr(settings(**{f"AVELIS_{name}": "21"}), name.lower()) == 21


@pytest.mark.parametrize("name", POSITIVE_FIELDS + ("PORT", "COLLECT_INTERVAL_MINUTES"))
@pytest.mark.parametrize("value", ["0", "-1", "1.5", "invalid", ""])
def test_positive_environment_rejects_invalid_values_with_key(name, value):
    key = f"AVELIS_{name}"
    with pytest.raises(ValueError, match=key):
        settings(**{key: value})


@pytest.mark.parametrize("name", NONNEGATIVE_FIELDS)
@pytest.mark.parametrize("value", ["-1", "1.5", "invalid", ""])
def test_nonnegative_environment_rejects_invalid_values_with_key(name, value):
    key = f"AVELIS_{name}"
    with pytest.raises(ValueError, match=key):
        settings(**{key: value})


@pytest.mark.parametrize("name", NONNEGATIVE_FIELDS)
def test_zero_preserves_pool_semantics_and_disables_token_limits(name):
    expected = None if name.endswith("TOKENS") else 0
    assert getattr(settings(**{f"AVELIS_{name}": "0"}), name.lower()) == expected


@pytest.mark.parametrize(("name", "maximum"), [("PORT", 65535), ("COLLECT_INTERVAL_MINUTES", 1440)])
def test_bounded_environment_accepts_boundaries_and_rejects_overflow(name, maximum):
    key = f"AVELIS_{name}"
    assert getattr(settings(**{key: "1"}), name.lower()) == 1
    assert getattr(settings(**{key: str(maximum)}), name.lower()) == maximum
    with pytest.raises(ValueError, match=key):
        settings(**{key: str(maximum + 1)})


@pytest.mark.parametrize("name", ["AUTO_MIGRATE", "AUTO_INIT_ADMIN"])
@pytest.mark.parametrize(("value", "expected"), [
    ("1", True), ("true", True), ("YES", True), (" On ", True),
    ("0", False), ("false", False), ("NO", False), (" Off ", False),
])
def test_boolean_environment_accepts_standard_spellings(name, value, expected):
    assert getattr(settings(**{f"AVELIS_{name}": value}), name.lower()) is expected


@pytest.mark.parametrize("name", ["AUTO_MIGRATE", "AUTO_INIT_ADMIN"])
@pytest.mark.parametrize("value", ["", "2", "enabled"])
def test_boolean_environment_rejects_other_spellings(name, value):
    key = f"AVELIS_{name}"
    with pytest.raises(ValueError, match=key):
        settings(**{key: value})


@pytest.mark.parametrize("value", ["critical", "error", "warning", "info", "debug", "trace", " DEBUG "])
def test_log_level_environment(value):
    assert settings(AVELIS_LOG_LEVEL=value).log_level == value.strip().lower()


@pytest.mark.parametrize("value", ["", "warn", "verbose"])
def test_log_level_rejects_unknown_values(value):
    with pytest.raises(ValueError, match="AVELIS_LOG_LEVEL"):
        settings(AVELIS_LOG_LEVEL=value)


@pytest.mark.parametrize("name", ["HOST", "FORWARDED_ALLOW_IPS"])
def test_service_strings_require_a_nonempty_value(name):
    key = f"AVELIS_{name}"
    assert getattr(settings(**{key: " 0.0.0.0 "}), name.lower()) == "0.0.0.0"
    with pytest.raises(ValueError, match=key):
        settings(**{key: " "})


def test_forwarded_allow_ips_accepts_uvicorn_trust_formats():
    for value in ("*", "127.0.0.1,10.0.0.0/8"):
        assert settings(AVELIS_FORWARDED_ALLOW_IPS=value).forwarded_allow_ips == value


def test_frontend_directory_resolves_relative_to_repo_root(tmp_path):
    assert settings(AVELIS_FRONTEND_DIR="frontend/dist").frontend_dir == (REPO_ROOT / "frontend/dist").resolve()
    assert settings(AVELIS_FRONTEND_DIR=str(tmp_path)).frontend_dir == tmp_path.resolve()
    assert settings(AVELIS_FRONTEND_DIR="").frontend_dir is None


def test_frontend_directory_resolution_errors_include_key_in_chinese(monkeypatch):
    for error in (ValueError("invalid path"), OSError("unavailable path")):
        with monkeypatch.context() as patch:
            patch.setattr(type(REPO_ROOT), "resolve", Mock(side_effect=error))
            with pytest.raises(ValueError, match="AVELIS_FRONTEND_DIR 必须为有效路径"):
                settings(AVELIS_FRONTEND_DIR="invalid")


def test_existing_environment_parsing_keeps_its_previous_acceptance():
    value = settings(
        AVELIS_ALLOW_SELF_REVIEW="yes", AVELIS_ASK_LIVE_CHECK="off", AVELIS_REQUIRE_COLUMN_COMMENTS="on",
        AVELIS_ANALYSIS_RETRIEVAL="on", AVELIS_ANALYSIS_INVESTIGATION="on", AVELIS_ANALYSIS_QUERY_LIMIT="-1",
        AVELIS_LOGIN_MAX_FAILURES="0", AVELIS_CORS_ORIGINS=" https://one.example,https://two.example ",
    )
    assert value.allow_self_review is False
    assert value.ask_live_check is True
    assert value.require_column_comments is False
    assert value.analysis_retrieval is False
    assert value.analysis_investigation is False
    assert value.analysis_query_limit == -1
    assert value.login_max_failures == 0
    assert value.cors_origins == ("https://one.example", "https://two.example")
