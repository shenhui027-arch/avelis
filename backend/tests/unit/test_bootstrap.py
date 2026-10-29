"""初始化命令中的账号与配置约定（不连数据库）。"""

from __future__ import annotations

import dataclasses

import pytest

from avelis import bootstrap
from avelis.config import REPO_ROOT, env_file, get_settings


def test_relative_env_file_resolves_against_repo_root(monkeypatch):
    monkeypatch.setenv("AVELIS_ENV_FILE", "local-admin.env")
    assert env_file() == REPO_ROOT / "local-admin.env"  # 与 scripts/demo.py 一致，不随工作目录变化
    absolute = REPO_ROOT / "x" / "y.env"
    monkeypatch.setenv("AVELIS_ENV_FILE", str(absolute))
    assert env_file() == absolute
    monkeypatch.delenv("AVELIS_ENV_FILE")
    assert env_file() == REPO_ROOT / ".env"


def _settings(url: str, admin_password: str):
    base = get_settings()
    return dataclasses.replace(base, database_url=url, env={**base.env, "AVELIS_ADMIN_PASSWORD": admin_password})


@pytest.mark.parametrize("url, local", [
    ("postgresql+psycopg://avelis:x@127.0.0.1:15432/avelis", True),
    ("postgresql+psycopg://avelis:x@localhost/avelis", True),
    ("postgresql+psycopg://avelis:x@meta.prod.internal:5432/avelis", False),
    ("postgresql+psycopg://avelis:x@10.0.0.8/avelis", False),
])
def test_superadmin_only_on_local_metadata_db(monkeypatch, url, local):
    monkeypatch.setattr(bootstrap, "get_settings", lambda: _settings(url, "123456"))
    assert bootstrap._local_metadata_db() is local
    if not local:
        # 弱密码超级管理员只用于本机元数据库：补齐时跳过，显式重设时拒绝；都不碰数据库
        assert bootstrap.ensure_superadmin() is None
        with pytest.raises(SystemExit, match="不在本机"):
            bootstrap.ensure_superadmin(reset_password=True)


def test_superadmin_skipped_without_configured_password(monkeypatch):
    monkeypatch.setattr(bootstrap, "get_settings",
                        lambda: _settings("postgresql+psycopg://a:b@127.0.0.1/avelis", ""))
    assert bootstrap.ensure_superadmin(reset_password=True) is None
