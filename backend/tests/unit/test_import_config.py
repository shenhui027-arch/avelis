"""批量导入的离线配置与模型结构检查。"""

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from avelis.config import ConfigError, build_settings
from avelis.configfile import read_config
from avelis.models import ImportBatch, ImportJob

SECRET = "offline-unit-test-secret-0123456789"


def test_import_size_defaults_and_environment_override():
    settings = build_settings({"AVELIS_SECRET": SECRET})
    assert settings.import_max_bytes == 20 * 1024**2
    assert settings.import_max_batch_bytes == 100 * 1024**2
    settings = build_settings(
        {"AVELIS_SECRET": SECRET, "AVELIS_IMPORT_MAX_BYTES": "17", "AVELIS_IMPORT_MAX_BATCH_BYTES": "29"}
    )
    assert (settings.import_max_bytes, settings.import_max_batch_bytes) == (17, 29)


@pytest.mark.parametrize("value", ["0", "-1", "invalid"])
def test_batch_limit_must_be_positive(value):
    with pytest.raises(ConfigError, match="AVELIS_IMPORT_MAX_BATCH_BYTES"):
        build_settings({"AVELIS_SECRET": SECRET, "AVELIS_IMPORT_MAX_BATCH_BYTES": value})


@pytest.mark.parametrize(("max_bytes", "max_batch_bytes"), [(17, 29), (0, 29), (17, -1)])
def test_import_toml_limits_share_positive_integer_validation(tmp_path, max_bytes, max_batch_bytes):
    path = tmp_path / "avelis.toml"
    path.write_text(
        f'[security]\nsecret = "{SECRET}"\n[import]\nmax_bytes = {max_bytes}\nmax_batch_bytes = {max_batch_bytes}\n',
        encoding="utf-8",
    )
    values = read_config({"AVELIS_CONFIG": str(path)})
    if min(max_bytes, max_batch_bytes) <= 0:
        key = "AVELIS_IMPORT_MAX_BYTES" if max_bytes <= 0 else "AVELIS_IMPORT_MAX_BATCH_BYTES"
        with pytest.raises(ConfigError, match=key):
            build_settings(values)
    else:
        settings = build_settings(values)
        assert (settings.import_max_bytes, settings.import_max_batch_bytes) == (max_bytes, max_batch_bytes)


def test_current_source_is_unique_without_limiting_historical_content():
    indexes = {i.name: i for i in ImportJob.__table__.indexes}
    current = str(CreateIndex(indexes["ix_import_job_current_source"]).compile(dialect=postgresql.dialect()))
    assert "UNIQUE" in current and "WHERE status <> 'superseded'" in current
    assert indexes["ix_import_job_content"].unique is False
    assert set(ImportBatch.__table__.columns.keys()) >= {"progress", "options", "stats", "notes", "job_id"}
