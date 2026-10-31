"""TOML、分层口令与部署检查命令的离线测试。"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from urllib.parse import unquote_plus, urlsplit

import pytest

from avelis import config, configfile

SECRET = "offline-unit-test-secret-0123456789"


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    """所有配置都来自测试临时文件，不读取机器上的部署配置。"""
    for key in tuple(os.environ):
        if key.startswith("AVELIS_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("AVELIS_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("AVELIS_SECRET", SECRET)
    monkeypatch.setattr(configfile, "DEFAULT_CONFIG_PATH", tmp_path / "absent.toml")


def write_toml(monkeypatch, tmp_path, content):
    path = tmp_path / "avelis.toml"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setenv("AVELIS_CONFIG", str(path))
    return path


def settings(**values):
    return config.build_settings({"AVELIS_SECRET": SECRET, **values})


def test_settings_priority_default_toml_dotenv_process(monkeypatch, tmp_path):
    assert config.build_settings().port == 8000
    write_toml(monkeypatch, tmp_path, "[server]\nport = 8101\n")
    assert config.build_settings().port == 8101
    dotenv = tmp_path / "test.env"
    dotenv.write_text("AVELIS_PORT=8102\n", encoding="utf-8")
    monkeypatch.setenv("AVELIS_ENV_FILE", str(dotenv))
    assert config.build_settings().port == 8102
    monkeypatch.setenv("AVELIS_PORT", "8103")
    assert config.build_settings().port == 8103


def test_explicit_environment_is_an_independent_input(monkeypatch, tmp_path):
    write_toml(monkeypatch, tmp_path, "[server]\nport = 8101\n")
    monkeypatch.setenv("AVELIS_PORT", "8102")
    assert settings().port == 8000


def test_missing_implicit_config_preserves_existing_defaults():
    value = config.build_settings()
    assert value.port == 8000 and value.public_url == ""


def test_existing_implicit_config_is_loaded(monkeypatch, tmp_path):
    path = tmp_path / "default.toml"
    path.write_text("[server]\nport = 8110\n", encoding="utf-8")
    monkeypatch.setattr(configfile, "DEFAULT_CONFIG_PATH", path)
    assert config.build_settings().port == 8110


def test_implicit_config_stat_permission_error_becomes_config_error(monkeypatch):
    default_path = configfile.DEFAULT_CONFIG_PATH
    original_exists = Path.exists

    def exists(path):
        if path == default_path:
            raise PermissionError("无权访问测试配置路径")
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", exists)
    with pytest.raises(config.ConfigError) as caught:
        config.build_settings()
    assert str(default_path) in str(caught.value)


def test_config_selector_only_comes_from_process(monkeypatch, tmp_path):
    dotenv = tmp_path / "test.env"
    dotenv.write_text(f"AVELIS_CONFIG={tmp_path / 'missing.toml'}\n", encoding="utf-8")
    monkeypatch.setenv("AVELIS_ENV_FILE", str(dotenv))
    assert config.build_settings().port == 8000


def test_explicit_config_path_is_relative_to_current_directory(monkeypatch, tmp_path):
    (tmp_path / "relative.toml").write_text("[server]\nport = 8120\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AVELIS_CONFIG", "relative.toml")
    assert config.build_settings().port == 8120


@pytest.mark.parametrize("content", [None, '[server]\nport = "unterminated\n'])
def test_explicit_missing_or_malformed_config_is_a_config_error(monkeypatch, tmp_path, content):
    path = tmp_path / "bad.toml"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    monkeypatch.setenv("AVELIS_CONFIG", str(path))
    with pytest.raises(config.ConfigError) as caught:
        config.build_settings()
    assert str(path) in str(caught.value)


def test_toml_types_are_flattened_and_reuse_settings_parsers(monkeypatch, tmp_path):
    write_toml(monkeypatch, tmp_path, '''
[server]
port = 8123
cors_origins = ["https://one.example", "https://two.example"]
[bootstrap]
auto_migrate = true
[analysis]
retrieval = true
material_change_ratio = 2
''')
    value = config.build_settings()
    assert value.port == 8123 and value.auto_migrate is True and value.analysis_retrieval is True
    assert value.material_change_ratio == 2.0
    assert value.cors_origins == ("https://one.example", "https://two.example")
    assert value.env["AVELIS_AUTO_MIGRATE"] == "true"
    assert value.env["AVELIS_CORS_ORIGINS"] == "https://one.example,https://two.example"


@pytest.mark.parametrize(("content", "key"), [
    ('[server]\nport = "abc"\n', "AVELIS_PORT"),
    ("[server]\nport = true\n", "AVELIS_PORT"),
    ('[server]\ncors_origins = "https://one.example"\n', "AVELIS_CORS_ORIGINS"),
    ('[server]\ncors_origins = ["https://one.example", 1]\n', "AVELIS_CORS_ORIGINS"),
    ("[server]\nhost = 12\n", "AVELIS_HOST"),
    ('[bootstrap]\nauto_migrate = "yes"\n', "AVELIS_AUTO_MIGRATE"),
    ("[analysis]\nmaterial_change_ratio = true\n", "AVELIS_MATERIAL_CHANGE_RATIO"),
])
def test_wrong_toml_types_are_rejected(monkeypatch, tmp_path, content, key):
    write_toml(monkeypatch, tmp_path, content)
    with pytest.raises(config.ConfigError, match=key):
        config.build_settings()


@pytest.mark.parametrize(("content", "unknown", "suggestion"), [
    ("[server]\nporrt = 8080\n", "porrt", "port"),
    ("[servre]\nport = 8080\n", "servre", "server"),
])
def test_unknown_toml_names_include_a_close_suggestion(monkeypatch, tmp_path, content, unknown, suggestion):
    write_toml(monkeypatch, tmp_path, content)
    with pytest.raises(config.ConfigError) as caught:
        config.build_settings()
    message = str(caught.value)
    assert unknown in message and suggestion in message
    assert "建议" in message


SENSITIVE_KEYS = [
    ("security", "secret", "AVELIS_SECRET"),
    ("database", "password", "AVELIS_DB_PASSWORD"),
    ("redis", "password", "AVELIS_REDIS_PASSWORD"),
    ("bootstrap", "initial_admin_password", "AVELIS_INITIAL_ADMIN_PASSWORD"),
    ("model", "api_key", "AVELIS_MODEL_API_KEY"),
    ("database", "url", "AVELIS_DATABASE_URL"),
    ("redis", "url", "AVELIS_REDIS_URL"),
]


@pytest.mark.parametrize(("section", "name", "key"), SENSITIVE_KEYS)
def test_toml_sensitive_file_reference(monkeypatch, tmp_path, section, name, key):
    secret_file = tmp_path / "credential"
    value = "postgresql+psycopg://u:p@host/db" if key == "AVELIS_DATABASE_URL" else "test-value-0123456789"
    secret_file.write_text(f"\n {value} \n", encoding="utf-8")
    if key == "AVELIS_SECRET":
        monkeypatch.delenv(key)
    write_toml(monkeypatch, tmp_path, f"[{section}]\n{name}_file = {secret_file.as_posix()!r}\n")
    assert config.build_settings().env[key] == value


def test_process_file_reference_overrides_lower_layer_plaintext(monkeypatch, tmp_path):
    write_toml(monkeypatch, tmp_path, '[database]\npassword = "config-password"\n')
    dotenv = tmp_path / "test.env"
    dotenv.write_text("AVELIS_DB_PASSWORD=dotenv-password\n", encoding="utf-8")
    monkeypatch.setenv("AVELIS_ENV_FILE", str(dotenv))
    secret_file = tmp_path / "credential"
    secret_file.write_text("  process-password\n", encoding="utf-8")
    monkeypatch.setenv("AVELIS_DB_PASSWORD_FILE", str(secret_file))
    assert config.build_settings().env["AVELIS_DB_PASSWORD"] == "process-password"


def test_dotenv_file_reference_overrides_config_plaintext(monkeypatch, tmp_path):
    write_toml(monkeypatch, tmp_path, '[redis]\npassword = "config-password"\n')
    secret_file = tmp_path / "credential"
    secret_file.write_text("  dotenv-password\n", encoding="utf-8")
    dotenv = tmp_path / "test.env"
    dotenv.write_text(f"AVELIS_REDIS_PASSWORD_FILE={secret_file}\n", encoding="utf-8")
    monkeypatch.setenv("AVELIS_ENV_FILE", str(dotenv))
    assert config.build_settings().env["AVELIS_REDIS_PASSWORD"] == "dotenv-password"


def test_process_plaintext_overrides_config_file_reference(monkeypatch, tmp_path):
    secret_file = tmp_path / "credential"
    secret_file.write_text("config-password", encoding="utf-8")
    write_toml(monkeypatch, tmp_path, f"[redis]\npassword_file = {secret_file.as_posix()!r}\n")
    monkeypatch.setenv("AVELIS_REDIS_PASSWORD", "process-password")
    assert config.build_settings().env["AVELIS_REDIS_PASSWORD"] == "process-password"


@pytest.mark.parametrize("layer", ["配置文件", ".env", "环境变量"])
def test_sensitive_plaintext_and_file_conflict_in_the_same_layer(monkeypatch, tmp_path, layer):
    secret_file = tmp_path / "credential"
    secret_file.write_text("file-password", encoding="utf-8")
    if layer == "配置文件":
        write_toml(monkeypatch, tmp_path,
                   f'[database]\npassword = "plain-password"\npassword_file = {secret_file.as_posix()!r}\n')
    elif layer == ".env":
        dotenv = tmp_path / "test.env"
        dotenv.write_text(f"AVELIS_DB_PASSWORD=plain-password\nAVELIS_DB_PASSWORD_FILE={secret_file}\n",
                          encoding="utf-8")
        monkeypatch.setenv("AVELIS_ENV_FILE", str(dotenv))
    else:
        monkeypatch.setenv("AVELIS_DB_PASSWORD", "plain-password")
        monkeypatch.setenv("AVELIS_DB_PASSWORD_FILE", str(secret_file))
    with pytest.raises(config.ConfigError) as caught:
        config.build_settings()
    message = str(caught.value)
    assert "AVELIS_DB_PASSWORD" in message and layer in message
    assert "plain-password" not in message and "file-password" not in message


@pytest.mark.parametrize("layer", ["配置文件", "环境变量"])
@pytest.mark.parametrize("is_directory", [False, True])
def test_missing_or_unreadable_secret_file_reports_key_and_path(monkeypatch, tmp_path, layer, is_directory):
    path = tmp_path / "missing-credential"
    if is_directory:
        path.mkdir()
    if layer == "配置文件":
        write_toml(monkeypatch, tmp_path, f"[redis]\npassword_file = {path.as_posix()!r}\n")
    else:
        monkeypatch.setenv("AVELIS_REDIS_PASSWORD_FILE", str(path))
    with pytest.raises(config.ConfigError) as caught:
        config.build_settings()
    assert "AVELIS_REDIS_PASSWORD" in str(caught.value)
    assert path.as_posix() in str(caught.value).replace("\\", "/")


def test_direct_environment_supports_secret_file_reference(tmp_path):
    path = tmp_path / "credential"
    path.write_text(f" {SECRET}\n", encoding="utf-8")
    assert config.build_settings({"AVELIS_SECRET_FILE": str(path)}).secret == SECRET


def test_explicit_connection_urls_win_over_components():
    database_url = "postgresql+psycopg://custom:pw@explicit/db?sslmode=require"
    redis_url = "redis://:pw@explicit:6379/6"
    value = settings(AVELIS_DATABASE_URL=database_url, AVELIS_DB_HOST="ignored", AVELIS_DB_PASSWORD="ignored",
                     AVELIS_REDIS_URL=redis_url, AVELIS_REDIS_HOST="ignored", AVELIS_REDIS_SSL="true")
    assert value.database_url == database_url and value.redis_url == redis_url


def test_database_components_encode_credentials_and_add_sslmode():
    password = "p@ss:/?#%+&=word"
    username = "user@name+suffix"
    value = settings(AVELIS_DB_HOST="db.internal", AVELIS_DB_PORT="5433", AVELIS_DB_NAME="metadata",
                     AVELIS_DB_USER=username, AVELIS_DB_PASSWORD=password, AVELIS_DB_SSLMODE="verify-full")
    url = urlsplit(value.database_url)
    assert url.scheme == "postgresql+psycopg" and url.hostname == "db.internal" and url.port == 5433
    assert unquote_plus(url.username) == username and unquote_plus(url.password) == password
    assert url.path == "/metadata" and url.query == "sslmode=verify-full"


def test_database_components_defaults_omit_empty_sslmode():
    value = settings(AVELIS_DB_HOST="db.internal", AVELIS_DB_PASSWORD="password")
    assert value.database_url == "postgresql+psycopg://avelis:password@db.internal:5432/avelis"


@pytest.mark.parametrize("sslmode", ["disable", "allow", "prefer", "require", "verify-ca", "verify-full"])
def test_database_sslmode_accepts_libpq_choices(sslmode):
    assert settings(AVELIS_DB_HOST="db.internal", AVELIS_DB_PASSWORD="password",
                    AVELIS_DB_SSLMODE=sslmode).database_url.endswith(f"?sslmode={sslmode}")


def test_database_components_require_password_and_valid_sslmode():
    with pytest.raises(config.ConfigError) as caught:
        settings(AVELIS_DB_HOST="db.internal", AVELIS_DB_PASSWORD="", AVELIS_DB_SSLMODE="invalid")
    assert "AVELIS_DB_PASSWORD" in str(caught.value) and "AVELIS_DB_SSLMODE" in str(caught.value)
    assert len(caught.value.problems) >= 2


def test_redis_components_encode_password_use_tls_and_database():
    password = "redis@:/?#%+&=word"
    url = urlsplit(settings(AVELIS_REDIS_HOST="redis.internal", AVELIS_REDIS_PORT="6380", AVELIS_REDIS_DB="4",
                           AVELIS_REDIS_SSL="true", AVELIS_REDIS_PASSWORD=password).redis_url)
    assert url.scheme == "rediss" and url.hostname == "redis.internal" and url.port == 6380
    assert unquote_plus(url.password) == password and url.path == "/4"


def test_no_hosts_or_urls_preserves_demo_connection_defaults():
    value = settings(AVELIS_PG_PORT="25432", AVELIS_PG_PASSWORD="pg@+password",
                     AVELIS_REDIS_PORT="26379", AVELIS_REDIS_PASSWORD="redis@+password")
    assert value.database_url == "postgresql+psycopg://avelis:pg%40%2Bpassword@127.0.0.1:25432/avelis"
    assert value.redis_url == "redis://:redis%40%2Bpassword@127.0.0.1:26379/0"


def test_public_url_is_normalized_and_added_to_cors_without_duplicate():
    value = settings(AVELIS_PUBLIC_URL="https://avelis.example/", AVELIS_CORS_ORIGINS="https://other.example")
    assert value.public_url == "https://avelis.example"
    assert value.cors_origins == ("https://other.example", "https://avelis.example")
    value = settings(AVELIS_PUBLIC_URL="https://avelis.example/", AVELIS_CORS_ORIGINS="https://avelis.example")
    assert value.cors_origins.count("https://avelis.example") == 1


def test_config_error_aggregates_problems_preserves_exception_types_and_hides_values():
    with pytest.raises(config.ConfigError) as caught:
        config.build_settings({"AVELIS_SECRET": "tiny-secret", "AVELIS_PORT": "bad-port",
                               "AVELIS_AUTO_MIGRATE": "bad-bool", "AVELIS_DB_HOST": "db.internal",
                               "AVELIS_DB_PASSWORD": "", "AVELIS_LOG_LEVEL": "bad-level"})
    error = caught.value
    assert isinstance(error, ValueError) and isinstance(error, RuntimeError)
    assert len(error.problems) >= 5
    message = str(error)
    for key in ("AVELIS_SECRET", "AVELIS_PORT", "AVELIS_AUTO_MIGRATE", "AVELIS_DB_PASSWORD", "AVELIS_LOG_LEVEL"):
        assert key in message
    assert "[security]" in message and "环境变量" in message
    assert "tiny-secret" not in message
    assert "AVELIS_DB_PASSWORD_FILE" in message


def test_repeated_settings_validation_does_not_mutate_loaded_problems(monkeypatch):
    monkeypatch.setenv("AVELIS_PORT", "bad")
    values = config.load_env()
    original_problems = list(values.problems)
    counts = []
    for _ in range(2):
        with pytest.raises(config.ConfigError) as caught:
            config.build_settings(values)
        counts.append(len(caught.value.problems))
        assert values.problems == original_problems
    assert counts[0] == counts[1] and counts[0] > 0


def test_sensitive_toml_type_error_never_echoes_the_value(monkeypatch, tmp_path):
    write_toml(monkeypatch, tmp_path, '[model]\napi_key = ["hidden-model-token"]\n')
    with pytest.raises(config.ConfigError) as caught:
        config.build_settings()
    assert "AVELIS_MODEL_API_KEY" in str(caught.value)
    assert "hidden-model-token" not in str(caught.value)


def test_toml_unassigned_asset_policy_rejects_unknown_choice(monkeypatch, tmp_path):
    write_toml(monkeypatch, tmp_path, '[business]\nunassigned_asset_policy = "custom-policy"\n')
    with pytest.raises(config.ConfigError, match="AVELIS_UNASSIGNED_ASSET_POLICY"):
        config.build_settings()


def test_existing_environment_unassigned_asset_policy_remains_unrestricted():
    assert settings(AVELIS_UNASSIGNED_ASSET_POLICY=" Custom-Policy ").unassigned_asset_policy == "custom-policy"


def test_existing_boolean_environment_keeps_accepting_arbitrary_strings():
    value = settings(AVELIS_ALLOW_SELF_REVIEW="previously-accepted", AVELIS_ASK_LIVE_CHECK="previously-accepted",
                     AVELIS_REQUIRE_COLUMN_COMMENTS="previously-accepted",
                     AVELIS_ANALYSIS_RETRIEVAL="previously-accepted", AVELIS_ANALYSIS_INVESTIGATION="previously-accepted",
                     AVELIS_MODEL_SEND_RESULTS="previously-accepted")
    assert value.allow_self_review is False and value.ask_live_check is True
    assert value.require_column_comments is False
    assert value.analysis_retrieval is False and value.analysis_investigation is False
    assert value.env["AVELIS_MODEL_SEND_RESULTS"] == "previously-accepted"


def test_config_check_prints_effective_sources_and_masks_all_sensitive_values(monkeypatch, tmp_path, capsys):
    write_toml(monkeypatch, tmp_path, "[server]\nport = 8181\n")
    dotenv = tmp_path / "test.env"
    dotenv.write_text("AVELIS_WORKERS=2\n", encoding="utf-8")
    monkeypatch.setenv("AVELIS_ENV_FILE", str(dotenv))
    secrets = {
        "AVELIS_SECRET": SECRET,
        "AVELIS_DB_PASSWORD": "db-hidden-password",
        "AVELIS_REDIS_PASSWORD": "redis-hidden-password",
        "AVELIS_INITIAL_ADMIN_PASSWORD": "admin-hidden-password",
        "AVELIS_MODEL_API_KEY": "model-hidden-api-key",
        "AVELIS_DATABASE_URL": "postgresql+psycopg://u:db-url-hidden@database/db",
        "AVELIS_REDIS_URL": "redis://:redis-url-hidden@redis/0",
    }
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)
    config.main(["check"])
    output = capsys.readouterr().out
    for key, value in secrets.items():
        assert key in output and value not in output
    for text in ("******", "（未设置）", "server", "port", "8181", "环境变量", ".env", "配置文件", "默认"):
        assert text in output


def test_config_check_reports_process_file_source_over_config_plaintext(monkeypatch, tmp_path, capsys):
    write_toml(monkeypatch, tmp_path, '[database]\npassword = "config-password"\n')
    secret_file = tmp_path / "credential"
    secret_file.write_text("process-password", encoding="utf-8")
    monkeypatch.setenv("AVELIS_DB_PASSWORD_FILE", str(secret_file))
    config.main(["check"])
    output = capsys.readouterr().out
    row = next(line for line in output.splitlines() if "AVELIS_DB_PASSWORD" in line)
    assert "环境变量" in row and "******" in row
    assert "process-password" not in output and "config-password" not in output


def test_config_cli_failure_has_exit_two_and_no_traceback(monkeypatch, tmp_path, capsys):
    path = write_toml(monkeypatch, tmp_path, '[server]\nport = "invalid"\n')
    monkeypatch.setenv("AVELIS_SECRET", "tiny-secret")
    with pytest.raises(SystemExit) as caught:
        config.main(["check"])
    assert caught.value.code == 2
    output = capsys.readouterr()
    assert "配置有误（共 " in output.err and "AVELIS_PORT" in output.err and "AVELIS_SECRET" in output.err
    assert str(path) in output.err and "Traceback" not in output.err and "tiny-secret" not in output.err


def test_export_service_secrets_writes_final_component_passwords(monkeypatch, tmp_path, capsys):
    write_toml(monkeypatch, tmp_path, '[database]\nhost = "postgres"\npassword = "config-password"\n'
                                      '[redis]\nhost = "redis"\n')
    monkeypatch.setenv("AVELIS_DB_PASSWORD", "effective-db@password")
    monkeypatch.setenv("AVELIS_REDIS_PASSWORD", "effective-redis password")
    directory = tmp_path / "new" / "service-secrets"
    config.main(["export-service-secrets", str(directory)])
    database, redis = directory / "postgres_password", directory / "redis_password"
    assert database.read_text(encoding="utf-8") == "effective-db@password"
    assert redis.read_text(encoding="utf-8") == "effective-redis password"
    if os.name != "nt":
        assert stat.S_IMODE(database.stat().st_mode) == 0o644
        assert stat.S_IMODE(redis.stat().st_mode) == 0o644
    output = capsys.readouterr().out
    assert "postgres_password" in output and "redis_password" in output
    assert "effective-db" not in output and "effective-redis" not in output


def test_export_service_secrets_takes_passwords_from_full_urls(monkeypatch, tmp_path):
    monkeypatch.setenv("AVELIS_DATABASE_URL", "postgresql+psycopg://avelis:url%40db@postgres:5432/avelis")
    monkeypatch.setenv("AVELIS_REDIS_URL", "redis://:url-redis@redis:6379/0")
    config.main(["export-service-secrets", str(tmp_path)])
    assert (tmp_path / "postgres_password").read_text(encoding="utf-8") == "url@db"
    assert (tmp_path / "redis_password").read_text(encoding="utf-8") == "url-redis"


def test_export_service_secrets_requires_passwords_for_bundled_services(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AVELIS_DB_HOST", "postgres")
    monkeypatch.setenv("AVELIS_DB_PASSWORD", "db-password")
    monkeypatch.setenv("AVELIS_REDIS_HOST", "redis")
    with pytest.raises(SystemExit) as caught:
        config.main(["export-service-secrets", str(tmp_path)])
    assert caught.value.code == 2
    assert "[redis] password" in capsys.readouterr().err
    assert not (tmp_path / "postgres_password").exists() and not (tmp_path / "redis_password").exists()


def test_export_service_secrets_with_external_services_only_validates(monkeypatch, tmp_path):
    monkeypatch.setenv("AVELIS_REDIS_URL", "redis://redis.internal:6379/0")
    config.main(["export-service-secrets", str(tmp_path), "--services", ""])
    assert list(tmp_path.iterdir()) == []


def test_higher_layer_overrides_unreadable_lower_layer_file_reference(monkeypatch, tmp_path):
    write_toml(monkeypatch, tmp_path, f'[security]\nsecret_file = "{(tmp_path / "missing").as_posix()}"\n')
    monkeypatch.setenv("AVELIS_SECRET", "process-secret-0123456789")
    assert config.build_settings().secret == "process-secret-0123456789"
    monkeypatch.delenv("AVELIS_SECRET")
    with pytest.raises(configfile.ConfigError, match="无法读取 AVELIS_SECRET_FILE"):
        config.build_settings()


def test_config_check_shows_standard_redis_port_for_component_connection(monkeypatch, capsys):
    monkeypatch.setenv("AVELIS_REDIS_HOST", "redis")
    monkeypatch.delenv("AVELIS_REDIS_PORT", raising=False)
    config.main(["check"])
    assert "redis.port | AVELIS_REDIS_PORT | 6379 |" in capsys.readouterr().out


def test_export_does_not_write_files_when_validation_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AVELIS_SECRET", "short-secret")
    monkeypatch.setenv("AVELIS_DB_PASSWORD", "hidden-password")
    directory = tmp_path / "service-secrets"
    with pytest.raises(SystemExit) as caught:
        config.main(["export-service-secrets", str(directory)])
    assert caught.value.code == 2
    assert not (directory / "postgres_password").exists()
    output = capsys.readouterr()
    assert "hidden-password" not in output.out + output.err and "short-secret" not in output.err


def test_redis_host_without_port_uses_standard_port():
    url = urlsplit(settings(AVELIS_REDIS_HOST="redis", AVELIS_REDIS_PASSWORD="pw").redis_url)
    assert (url.hostname, url.port) == ("redis", 6379)
