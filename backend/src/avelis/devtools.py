"""开发与测试工具。只用于本地演示元数据库，不在服务进程中调用。"""

from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from avelis.config import REPO_ROOT
from avelis.db import get_engine


def alembic_config() -> Config:
    return Config(str(REPO_ROOT / "backend" / "alembic.ini"))


def reset_metadata_db() -> None:
    """删除元数据库中的全部表并迁移到最新版本。"""
    engine = get_engine()
    with engine.begin() as conn:
        tables = inspect(conn).get_table_names()
        if engine.dialect.name == "mysql":
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
            for table in tables:
                conn.execute(text(f"DROP TABLE IF EXISTS `{table}`"))
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        else:
            for table in tables:
                conn.execute(text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))
    command.upgrade(alembic_config(), "head")
