"""MySQL / StarRocks 替身必须遵守实际 PyMySQL 参数绑定，避免遗漏百分号错误。"""

from __future__ import annotations

import pymysql
import pytest

from tests.pymysql_contract import mogrify
from tests.unit.test_mysql_audit import _Cursor as MySQLAuditCursor
from tests.unit.test_mysql_query_lineage import Connection as MySQLQueryConnection
from tests.unit.test_mysql_query_lineage import Cursor as MySQLQueryCursor
from tests.unit.test_overfit_batch2 import conn_Connection, conn_Cursor
from tests.unit.test_starrocks_audit import _Cursor as StarRocksAuditCursor


@pytest.fixture(params=["mysql_audit", "starrocks_audit", "mysql_query", "shared_query"])
def cursor(request):
    if request.param == "mysql_audit":
        return MySQLAuditCursor([])
    if request.param == "starrocks_audit":
        return StarRocksAuditCursor([], [], "UTC")
    if request.param == "mysql_query":
        return MySQLQueryCursor(MySQLQueryConnection())
    return conn_Cursor(conn_Connection())


QUERY = "SELECT event_time, UNIX_TIMESTAMP(event_time) FROM audit WHERE argument LIKE '%CREATE%'"


@pytest.mark.parametrize("args", [(), {}, ("alice",), {"actor": "alice"}])
def test_fake_cursors_reject_unescaped_percent_with_any_arguments(cursor, args):
    with pytest.raises((pymysql.ProgrammingError, ValueError)):
        cursor.execute(QUERY, args)


def test_fake_cursors_accept_percent_without_arguments_and_escaped_bound_queries(cursor):
    cursor.execute(QUERY)
    cursor.execute(QUERY.replace("%", "%%"), ())
    cursor.execute(QUERY.replace("%", "%%") + " AND user_host = %s", ("100% %(literal)s",))
    cursor.execute(QUERY.replace("%", "%%") + " AND user_host = %(actor)s", {"actor": "alice%"})


def test_mogrify_uses_real_escaping_without_connecting_or_reformatting_values(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("参数格式化不能建立数据库连接")

    monkeypatch.setattr(pymysql.connections.Connection, "connect", forbidden)
    assert mogrify(QUERY) == QUERY
    assert mogrify(QUERY.replace("%", "%%"), ()) == QUERY
    assert mogrify(QUERY.replace("%", "%%"), {}) == QUERY
    assert mogrify("SELECT %s", ("100% %(literal)s",)) == "SELECT '100% %(literal)s'"
    assert mogrify("SELECT %(actor)s", {"actor": "O'Reilly\\100%"}) == r"SELECT 'O\'Reilly\\100%'"
