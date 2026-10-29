"""Alembic 环境：连接串来自 avelis.config，PostgreSQL 与 MySQL 共用同一套迁移。"""

from alembic import context

from avelis import models  # noqa: F401 - 注册全部表
from avelis.db import Base, get_engine

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=str(get_engine().url), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = context.config.attributes.get("connection") or get_engine()
    if hasattr(connectable, "connect") and not hasattr(connectable, "execute"):
        with connectable.connect() as connection:
            _run(connection)
    else:
        _run(connectable)


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
