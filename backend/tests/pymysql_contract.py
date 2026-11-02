"""离线游标替身共用真实 PyMySQL 参数格式化，保留驱动的百分号与转义语义。"""

from __future__ import annotations

from pymysql.connections import Connection
from pymysql.cursors import Cursor


def mogrify(query, args=None):
    """只格式化，不连接；替身仍可记录原始 SQL 和参数供断言。"""
    if args is None:
        return query
    connection = Connection(defer_connect=True)
    connection.server_status = 0
    return Cursor(connection).mogrify(query, args)
