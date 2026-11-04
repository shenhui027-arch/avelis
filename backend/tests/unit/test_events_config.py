"""Event outbox retention configuration uses the shared TOML and environment loader."""

import pytest

from avelis.config import ConfigError, build_settings
from avelis.configfile import ConfigValues, merge_layers, read_config

SECRET = "offline-unit-test-secret-0123456789"
RETENTION_KEY = "AVELIS_EVENTS_DELIVERED_RETENTION_DAYS"


def test_delivered_retention_defaults_to_thirty_days():
    settings = build_settings({"AVELIS_SECRET": SECRET})

    assert settings.events_delivered_retention_days == 30


@pytest.mark.parametrize("days", ["7", "90"])
def test_delivered_retention_environment_override(days):
    settings = build_settings({"AVELIS_SECRET": SECRET, RETENTION_KEY: days})

    assert settings.events_delivered_retention_days == int(days)


def test_zero_disables_delivered_retention():
    settings = build_settings({"AVELIS_SECRET": SECRET, RETENTION_KEY: "0"})

    assert settings.events_delivered_retention_days == 0


@pytest.mark.parametrize("value", ["-1", "invalid", "1.5"])
def test_invalid_delivered_retention_is_reported_with_other_configuration_errors(value):
    with pytest.raises(ConfigError) as caught:
        build_settings(
            {
                "AVELIS_SECRET": SECRET,
                RETENTION_KEY: value,
                "AVELIS_WEBHOOK_MAX_ATTEMPTS": "0",
            }
        )

    assert any(RETENTION_KEY in problem for problem in caught.value.problems)
    assert any("AVELIS_WEBHOOK_MAX_ATTEMPTS" in problem for problem in caught.value.problems)


@pytest.mark.parametrize("days", [0, 45])
def test_delivered_retention_toml_configuration(tmp_path, days):
    path = tmp_path / "avelis.toml"
    path.write_text(
        f'[security]\nsecret = "{SECRET}"\n[events]\ndelivered_retention_days = {days}\n',
        encoding="utf-8",
    )

    settings = build_settings(read_config({"AVELIS_CONFIG": str(path)}))

    assert settings.events_delivered_retention_days == days


def test_delivered_retention_environment_overrides_toml(tmp_path):
    path = tmp_path / "avelis.toml"
    path.write_text(
        f'[security]\nsecret = "{SECRET}"\n[events]\ndelivered_retention_days = 45\n',
        encoding="utf-8",
    )

    settings = build_settings(
        merge_layers(read_config({"AVELIS_CONFIG": str(path)}), ConfigValues({RETENTION_KEY: "7"}))
    )

    assert settings.events_delivered_retention_days == 7


def test_dead_retention_defaults_to_ninety_days():
    assert build_settings({"AVELIS_SECRET": SECRET}).events_dead_retention_days == 90


@pytest.mark.parametrize("days", ["0", "7", "180"])
def test_dead_retention_environment_override(days):
    settings = build_settings({"AVELIS_SECRET": SECRET, "AVELIS_EVENTS_DEAD_RETENTION_DAYS": days})

    assert settings.events_dead_retention_days == int(days)


@pytest.mark.parametrize("value", ["-1", "invalid", "1.5"])
def test_invalid_dead_retention(value):
    with pytest.raises(ConfigError, match="AVELIS_EVENTS_DEAD_RETENTION_DAYS"):
        build_settings({"AVELIS_SECRET": SECRET, "AVELIS_EVENTS_DEAD_RETENTION_DAYS": value})


@pytest.mark.parametrize("days", [0, 45])
def test_dead_retention_toml_and_environment_precedence(tmp_path, days):
    path = tmp_path / "avelis.toml"
    path.write_text(
        f'[security]\nsecret = "{SECRET}"\n[events]\ndead_retention_days = {days}\n',
        encoding="utf-8",
    )
    toml = read_config({"AVELIS_CONFIG": str(path)})

    assert build_settings(toml).events_dead_retention_days == days
    overridden = merge_layers(toml, ConfigValues({"AVELIS_EVENTS_DEAD_RETENTION_DAYS": "7"}))
    assert build_settings(overridden).events_dead_retention_days == 7
