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
    """删除元数据库中的全部表（含智能体会话状态）并迁移到最新版本。"""
    engine = get_engine()
    with engine.begin() as conn:
        for table in inspect(conn).get_table_names():
            conn.execute(text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))
        conn.execute(text("DROP SCHEMA IF EXISTS avelis_agent CASCADE"))
    command.upgrade(alembic_config(), "head")
    from avelis.ask.agent import graph, reset_state_cache

    reset_state_cache()
    # 立即建好智能体状态表：LangGraph 建表时用 CREATE INDEX CONCURRENTLY，会等待所有已有写入的事务；
    # 若推迟到某个请求里（该请求已写入数据）才建，会等待请求自己的事务而卡死
    graph()
