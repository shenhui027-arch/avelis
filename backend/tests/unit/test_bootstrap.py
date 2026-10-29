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


def _settings(admin_password: str):
    base = get_settings()
    return dataclasses.replace(base, env={**base.env, "AVELIS_ADMIN_PASSWORD": admin_password})


@pytest.mark.parametrize("host, hostaddr, local", [
    # 连接实际使用的目标（libpq 解析连接串、服务文件、环境变量、多主机之后）：TCP 看实际 IP
    ("127.0.0.1", "127.0.0.1", True),
    ("localhost", "::1", True),
    ("localhost", "0:0:0:0:0:0:0:1", True),
    ("127.0.0.2", "127.0.0.2", True),
    ("localhost", "10.0.0.8", False),  # 名字是 localhost，实际连到了远端（例如被 hostaddr / 服务文件覆盖）
    ("meta.prod.internal", "10.0.0.8", False),
    ("/var/run/postgresql", None, True),  # Unix socket
    ("/var/run/postgresql", "", True),
    ("meta.prod.internal", None, False),
    (None, None, False),
])
def test_local_target_uses_actual_connection_address(host, hostaddr, local):
    assert bootstrap.is_local_target(host, hostaddr) is local


def test_superadmin_refused_on_non_local_connection_without_writing(monkeypatch):
    from contextlib import contextmanager

    writes = []

    class Session:
        def add(self, obj):
            writes.append(obj)

        def scalar(self, *_):
            raise AssertionError("非本机连接上不应查询或写入")

    @contextmanager
    def fake_scope():
        yield Session()

    monkeypatch.setattr(bootstrap, "get_settings", lambda: _settings("123456"))
    monkeypatch.setattr(bootstrap, "session_scope", fake_scope)
    monkeypatch.setattr(bootstrap, "_connection_is_local", lambda session: False)
    assert bootstrap.ensure_superadmin() is None  # 例行补齐：跳过
    with pytest.raises(SystemExit, match="不在本机"):
        bootstrap.ensure_superadmin(reset_password=True)  # 显式重设：拒绝
    assert writes == []


def test_superadmin_skipped_without_configured_password(monkeypatch):
    monkeypatch.setattr(bootstrap, "get_settings", lambda: _settings(""))
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
