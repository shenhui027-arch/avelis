"""元数据库（PostgreSQL 18）连接与公共类型。

- 主键由应用生成（32 位十六进制字符串）；
- JSON 用 JSONB；
- 时间统一存 UTC、不带时区；
- 状态值存短字符串，由应用校验，不用数据库原生枚举。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache

from sqlalchemy import DateTime, MetaData, String, Text, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from avelis.cache import LockBusy
from avelis.config import get_settings

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class UTCDateTime(TypeDecorator):
    """存储为不带时区的 UTC 时间；读出时补上 UTC 时区。"""

    impl = DateTime(timezone=False)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


LongText = Text()
JsonType = JSONB()
IdType = String(32)


@lru_cache(maxsize=4)
def _engine(url: str, pool_size: int = 5, max_overflow: int = 10, pool_recycle_seconds: int = 1800) -> Engine:
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_recycle=pool_recycle_seconds,
        future=True,
    )


def get_engine() -> Engine:
    settings = get_settings()
    return _engine(
        settings.database_url, settings.db_pool_size, settings.db_max_overflow, settings.db_pool_recycle_seconds
    )


@contextmanager
def advisory_lock(key: str) -> Iterator[None]:
    """独占连接持有会话锁，直到采集退出；连接断开时 PostgreSQL 自动释放。"""
    with get_engine().connect() as connection:
        connection = connection.execution_options(isolation_level="AUTOCOMMIT")
        acquired = connection.scalar(text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"), {"key": key})
        if not acquired:
            raise LockBusy(key)
        try:
            yield
        finally:
            try:
                connection.scalar(text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"), {"key": key})
            except BaseException:
                # 不把可能仍持锁的连接交还连接池。
                connection.invalidate()
                raise


@lru_cache(maxsize=4)
def _session_factory(
    url: str, pool_size: int = 5, max_overflow: int = 10, pool_recycle_seconds: int = 1800
) -> sessionmaker[Session]:
    return sessionmaker(
        bind=_engine(url, pool_size, max_overflow, pool_recycle_seconds), expire_on_commit=False, future=True
    )


def session_factory() -> sessionmaker[Session]:
    settings = get_settings()
    return _session_factory(
        settings.database_url, settings.db_pool_size, settings.db_max_overflow, settings.db_pool_recycle_seconds
    )


@contextmanager
def session_scope() -> Iterator[Session]:
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
