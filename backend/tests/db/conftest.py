"""需要元数据库的测试（-m db 可单独运行；数据库不可用时跳过）。

在同一 PostgreSQL 实例上新建独立的 avelis_m8_test 库并迁移到最新版本，测试只读写这个库，不触碰共用的元数据库。
连接信息来自 .env / 进程环境变量，不打印。
"""

from __future__ import annotations

import os
import uuid

import pytest

TEST_DB = "avelis_m8_test"


@pytest.fixture(scope="session")
def meta_db():
    import psycopg
    from alembic import command
    from sqlalchemy.engine import make_url

    from avelis import config
    from avelis.devtools import alembic_config

    try:
        base = make_url(config.get_settings().database_url)
    except Exception as exc:  # noqa: BLE001 - 没有 .env 等
        pytest.skip(f"没有元数据库配置：{type(exc).__name__}")
    try:
        with psycopg.connect(host=base.host, port=base.port, user=base.username, password=base.password,
                             dbname="postgres", autocommit=True, connect_timeout=3) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
            conn.execute(f"CREATE DATABASE {TEST_DB}")
    except psycopg.OperationalError:
        pytest.skip("元数据库不可用")
    previous = os.environ.get("AVELIS_DATABASE_URL")
    os.environ["AVELIS_DATABASE_URL"] = base.set(database=TEST_DB).render_as_string(hide_password=False)
    config.get_settings.cache_clear()
    assert make_url(config.get_settings().database_url).database == TEST_DB
    command.upgrade(alembic_config(), "head")
    yield
    if previous is None:
        os.environ.pop("AVELIS_DATABASE_URL", None)
    else:
        os.environ["AVELIS_DATABASE_URL"] = previous
    config.get_settings.cache_clear()


@pytest.fixture
def factory(meta_db):
    """建测试数据的小工具：用户（角色）、业务域、指标。名称带随机后缀，测试之间互不影响。"""
    from avelis.db import session_scope
    from avelis.models import Domain, DomainMember, Metric, User, UserRole

    class Factory:
        @staticmethod
        def user(roles=("analyst",), active=True) -> str:
            with session_scope() as s:
                u = User(username=f"u{uuid.uuid4().hex[:10]}", display_name="测试", password_hash="x",
                         is_active=active)
                s.add(u)
                s.flush()
                for r in roles:
                    s.add(UserRole(user_id=u.id, role=r))
                return u.id

        @staticmethod
        def metric(owner_id: str, member_id: str | None = None) -> str:
            """一个业务域里的指标（没有发布的版本）；member_id 为该域成员。返回指标编码。"""
            with session_scope() as s:
                d = Domain(code=f"d{uuid.uuid4().hex[:8]}", name="测试域", owner_id=owner_id)
                s.add(d)
                s.flush()
                if member_id:
                    s.add(DomainMember(domain_id=d.id, user_id=member_id, role="viewer"))
                m = Metric(code=f"m{uuid.uuid4().hex[:8]}", name="测试指标", domain_id=d.id, owner_id=owner_id,
                           created_by=owner_id)
                s.add(m)
                return m.code

    return Factory()
