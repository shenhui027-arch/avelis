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
    # psycopg 用查询参数覆盖 URL 中的主机：按实际生效的目标判断
    ("postgresql+psycopg://avelis:x@localhost/avelis?host=meta.prod.internal", False),
    ("postgresql+psycopg://avelis:x@localhost/avelis?hostaddr=10.0.0.8", False),
    ("postgresql+psycopg://avelis:x@localhost/avelis?host=localhost,meta.prod.internal", False),
    ("postgresql+psycopg://avelis:x@/avelis?host=/var/run/postgresql", True),  # 本机 Unix socket
    ("postgresql+psycopg://avelis:x@[0:0:0:0:0:0:0:1]/avelis", True),
    ("postgresql+psycopg://avelis:x@127.0.0.2/avelis", True),
    ("postgresql+psycopg://avelis:x@/avelis", False),  # 目标来自 PGHOST 等外部配置，无法确定
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


def test_session_cutoff_distinguishes_tokens_within_the_same_second():
    from datetime import datetime, timezone

    from avelis.security import issued_before

    cutoff = datetime(2026, 10, 30, 8, 0, 0, 500_000, tzinfo=timezone.utc)
    second = int(cutoff.timestamp())
    us = second * 1_000_000 + cutoff.microsecond
    assert issued_before({"iat": second, "iat_us": us - 400_000}, cutoff)  # 同一秒、改密前签发：失效
    assert not issued_before({"iat": second, "iat_us": us + 1}, cutoff)  # 同一秒、改密后签发：有效
    # 没有 iat_us 的旧令牌：同一秒内的一律失效，下一秒签发的有效
    assert issued_before({"iat": second}, cutoff)
    assert not issued_before({"iat": second + 1}, cutoff)
