"""MySQL 的真实驱动契约和现有 SQLGlot 链路；不连接外部数据库。"""

from __future__ import annotations

from datetime import datetime

import pymysql
import pytest

from avelis.ask.compiler import ValidationFailed, validate_sql
from avelis.connectors.base import QueryError
from avelis.connectors.mysql import MySQLMetaConnector
from avelis.engines.builtin import MYSQL
from avelis.lineage.sql import canonical_hash_of, parse_query
from tests.pymysql_contract import mogrify


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = [("value",)]
        self.rows = []

    def execute(self, sql, params=None):
        mogrify(sql, params)
        self.connection.statements.append((sql, params))
        if self.connection.fail_at and self.connection.fail_at in sql:
            raise self.connection.error
        self.rows = self.connection.respond(sql, params)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def fetchmany(self, count):
        self.connection.fetch_counts.append(count)
        return self.rows[:count]


class Connection:
    def __init__(self, respond=lambda sql, params: [], *, fail_at=None, error=None, rollback_error=None):
        self.respond = respond
        self.fail_at, self.error, self.rollback_error = fail_at, error, rollback_error
        self.statements, self.fetch_counts, self.cursor_types = [], [], []
        self.closed, self.rollbacks = False, 0

    def cursor(self, *args):
        self.cursor_types.append(args)
        return Cursor(self)

    def rollback(self):
        self.rollbacks += 1
        if self.rollback_error:
            raise self.rollback_error

    def close(self):
        self.closed = True


def connector(options=None):
    return MySQLMetaConnector("unit-host", 3306, "read-user", "unit-password", options or {"databases": ["sample"]})


def test_connect_uses_bounded_driver_timeouts_and_no_multi_statements(monkeypatch):
    calls = []
    monkeypatch.setattr(pymysql, "connect", lambda **kwargs: calls.append(kwargs))
    connector()._connect(read_timeout=12)
    assert calls == [{"host": "unit-host", "port": 3306, "user": "read-user", "password": "unit-password",
                      "autocommit": True, "charset": "utf8mb4", "connect_timeout": 5, "read_timeout": 12,
                      "write_timeout": 30, "cursorclass": pymysql.cursors.DictCursor}]


def test_query_read_only_timeout_row_limit_and_cleanup(monkeypatch):
    connection = Connection(lambda sql, params: [(1,), (2,), (3,)] if sql == "SELECT value FROM sample.source" else [])
    adapter, timeouts = connector(), []

    def connect(**kwargs):
        timeouts.append(kwargs)
        return connection

    monkeypatch.setattr(adapter, "_connect", connect)
    result = adapter.execute_select("SELECT value FROM sample.source", timeout_seconds=3,
                                    mem_limit_bytes=1024, max_rows=2, timezone="UTC")
    assert result.columns == ["value"] and result.rows == [[1], [2]] and result.truncated
    assert result.warnings == []
    assert timeouts == [{"read_timeout": 18}]
    assert connection.statements == [
        ("SET time_zone = 'UTC'", None), ("SET SESSION MAX_EXECUTION_TIME = 3000", None),
        ("SET SESSION SQL_SELECT_LIMIT = 3", None), ("START TRANSACTION READ ONLY", None),
        ("SELECT value FROM sample.source", None)]
    assert connection.cursor_types == [(pymysql.cursors.Cursor,)]
    assert connection.fetch_counts == [3] and connection.rollbacks == 1 and connection.closed


def test_query_exact_limit_and_zero_rows_are_not_misreported(monkeypatch):
    adapter = connector()
    for max_rows, rows, expected, truncated in ((2, [(1,), (2,)], [[1], [2]], False),
                                                 (0, [(1,)], [], True), (2, [], [], False)):
        connection = Connection(lambda sql, params, rows=rows: rows if sql == "SELECT 1" else [])
        monkeypatch.setattr(adapter, "_connect", lambda **kwargs: connection)
        result = adapter.execute_select("SELECT 1", timeout_seconds=1, mem_limit_bytes=0, max_rows=max_rows)
        assert result.rows == expected and result.truncated is truncated
        assert connection.fetch_counts == [max_rows + 1] and connection.closed


@pytest.mark.parametrize("timeout,max_rows,code", [(0, 1, "timeout"), (-1, 1, "timeout"), (1, -1, "other")])
def test_invalid_query_limits_fail_before_connect(monkeypatch, timeout, max_rows, code):
    adapter = connector()
    monkeypatch.setattr(adapter, "_connect", lambda **kwargs: pytest.fail("must not connect"))
    with pytest.raises(QueryError) as raised:
        adapter.execute_select("SELECT 1", timeout_seconds=timeout, mem_limit_bytes=0, max_rows=max_rows)
    assert raised.value.code == code


@pytest.mark.parametrize("errno,message,expected", [
    (3024, "Query execution was interrupted, maximum statement execution time exceeded", "timeout"),
    (1205, "Lock wait timeout exceeded", "timeout"), (1317, "Query execution was interrupted", "timeout"),
    (2013, "Lost connection to MySQL server during query (timed out)", "timeout"),
    (1792, "Cannot execute statement in a READ ONLY transaction", "permission"),
    (1045, "Access denied for user", "permission"), (1142, "SELECT command denied", "permission"),
    (1143, "Column command denied", "permission"), (1290, "Server is running with the read-only option", "permission"),
    (1054, "Unknown column", "invalid_object"), (1146, "Table does not exist", "invalid_object"),
    (1049, "Unknown database", "invalid_object"), (1064, "Syntax error", "other"),
])
def test_query_database_errors_preserve_details_and_cleanup(monkeypatch, errno, message, expected):
    error = pymysql.err.OperationalError(errno, message)
    connection = Connection(fail_at="SELECT 1", error=error,
                            rollback_error=pymysql.err.OperationalError(2006, "connection lost during cleanup"))
    adapter = connector()
    monkeypatch.setattr(adapter, "_connect", lambda **kwargs: connection)
    with pytest.raises(QueryError) as raised:
        adapter.execute_select("SELECT 1", timeout_seconds=2, mem_limit_bytes=0, max_rows=5)
    assert (raised.value.code, raised.value.db_errno, raised.value.message) == (expected, errno, message)
    assert raised.value.__cause__ is error
    assert connection.rollbacks == 1 and connection.closed


def test_query_does_not_execute_when_read_only_transaction_fails(monkeypatch):
    connection = Connection(fail_at="START TRANSACTION READ ONLY",
                            error=pymysql.err.OperationalError(1142, "command denied"))
    adapter = connector()
    monkeypatch.setattr(adapter, "_connect", lambda **kwargs: connection)
    with pytest.raises(QueryError, match="command denied"):
        adapter.execute_select("SELECT 1", timeout_seconds=2, mem_limit_bytes=0, max_rows=5)
    assert all(sql != "SELECT 1" for sql, _ in connection.statements)
    assert connection.closed and connection.rollbacks == 0


def test_query_connection_failure_is_query_error(monkeypatch):
    adapter = connector()

    def fail(**kwargs):
        raise pymysql.err.OperationalError(1045, "Access denied for read-user")

    monkeypatch.setattr(adapter, "_connect", fail)
    with pytest.raises(QueryError) as raised:
        adapter.execute_select("SELECT 1", timeout_seconds=2, mem_limit_bytes=0, max_rows=5)
    assert raised.value.code == "permission" and raised.value.db_errno == 1045


@pytest.mark.parametrize("sql", ["DROP TABLE sample.source", "DELETE FROM sample.source", "SELECT 1; SELECT 2",
                                "SELECT id FROM sample.source", "SELECT id FROM sample.source LIMIT 99",
                                "SELECT id INTO OUTFILE '/tmp/ignored' FROM sample.source LIMIT 2"])
def test_mysql_query_uses_existing_call_layer_sql_guard(sql):
    with pytest.raises(ValidationFailed):
        validate_sql(sql, dialect=MYSQL.sql, allowed_tables={"def.sample.source"},
                     schema={"def.sample.source": {"id"}}, max_rows=2)


def test_live_check_parameterizes_exact_pairs_and_preserves_definition(monkeypatch):
    definition = "select `sample`.`source`.`id` AS `id` from `sample`.`source`"

    def respond(sql, params):
        if "information_schema.columns" in sql:
            return [{"TABLE_SCHEMA": "sample", "TABLE_NAME": "view", "COLUMN_NAME": "b",
                     "ORDINAL_POSITION": 2, "COLUMN_TYPE": "varchar(12)"},
                    {"TABLE_SCHEMA": "sample", "TABLE_NAME": "view", "COLUMN_NAME": "a",
                     "ORDINAL_POSITION": 1, "COLUMN_TYPE": "int"},
                    {"TABLE_SCHEMA": "other", "TABLE_NAME": "view", "COLUMN_NAME": "ignored",
                     "ORDINAL_POSITION": 1, "COLUMN_TYPE": "int"}]
        if "information_schema.views" in sql:
            return [{"TABLE_SCHEMA": "sample", "TABLE_NAME": "view", "VIEW_DEFINITION": definition}]
        return []

    adapter, connection, timeouts = connector(), Connection(respond), []

    def connect(**kwargs):
        timeouts.append(kwargs)
        return connection

    monkeypatch.setattr(adapter, "_connect", connect)
    names = [("sample", "view"), ("sample", "missing"), ("sample", "strange`'; SELECT 1")]
    result = adapter.describe_objects(names, timeout_seconds=4)
    assert set(result) == set(names)
    assert result[("sample", "view")] == {"columns": [["a", "int"], ["b", "varchar(12)"]],
                                            "definition": definition, "physical_id": None}
    assert result[("sample", "missing")] == {"columns": [], "definition": None, "physical_id": None}
    assert timeouts == [{"read_timeout": 4}] and connection.closed
    assert connection.statements[0] == ("SET SESSION MAX_EXECUTION_TIME = 4000", None)
    for sql, params in connection.statements[1:]:
        assert "strange" not in sql and sql.endswith("(TABLE_SCHEMA, TABLE_NAME) IN ((%s,%s),(%s,%s),(%s,%s))")
        assert params == tuple(value for pair in sorted(names) for value in pair)


def test_empty_live_check_and_freshness_do_not_connect(monkeypatch):
    adapter = connector()
    monkeypatch.setattr(adapter, "_connect", lambda **kwargs: pytest.fail("must not connect"))
    assert adapter.describe_objects([]) == {}
    assert adapter.freshness([("sample", "source")]) == {}
    with pytest.raises(QueryError) as raised:
        adapter.describe_objects([("sample", "source")], timeout_seconds=0)
    assert raised.value.code == "timeout"


@pytest.mark.parametrize("connect_failure", [False, True])
def test_live_check_permission_errors_are_classified(monkeypatch, connect_failure):
    error = pymysql.err.OperationalError(1142, "SHOW VIEW command denied")
    connection = Connection(fail_at="information_schema.views", error=error)
    adapter = connector()

    def connect(**kwargs):
        if connect_failure:
            raise error
        return connection

    monkeypatch.setattr(adapter, "_connect", connect)
    with pytest.raises(QueryError) as raised:
        adapter.describe_objects([("sample", "view")])
    assert raised.value.code == "permission" and raised.value.message == "SHOW VIEW command denied"
    assert connection.closed is not connect_failure


REWRITTEN = (
    "select `sample`.`source`.`id` AS `id`,sum((`sample`.`source`.`amount` - "
    "`sample`.`source`.`discount`)) AS `total` from `sample`.`source` "
    "where (`sample`.`source`.`active` = 1) group by `sample`.`source`.`id`"
)


def snapshot_connection(definition):
    def respond(sql, params):
        if "@@server_uuid" in sql:
            return [{"server_uuid": "mysql-unit-uuid", "hostname": "unit-host", "port": 3306, "version": "8.4"}]
        if "information_schema.tables" in sql:
            return [{"TABLE_SCHEMA": "sample", "TABLE_NAME": name, "TABLE_TYPE": kind,
                     "TABLE_COMMENT": "", "CREATE_TIME": None}
                    for name, kind in (("source", "BASE TABLE"), ("summary", "VIEW"))]
        if "information_schema.columns" in sql:
            return [{"TABLE_SCHEMA": "sample", "TABLE_NAME": name, "COLUMN_NAME": col,
                     "ORDINAL_POSITION": i + 1, "COLUMN_TYPE": dtype, "IS_NULLABLE": "NO",
                     "COLUMN_COMMENT": "", "COLUMN_KEY": "PRI" if col == "id" and name == "source" else ""}
                    for name, cols in (("source", [("id", "int"), ("amount", "decimal(18,2)"),
                                                  ("discount", "decimal(18,2)"), ("active", "int")]),
                                       ("summary", [("id", "int"), ("total", "decimal(18,2)")]))
                    for i, (col, dtype) in enumerate(cols)]
        if "information_schema.statistics" in sql:
            return [{"TABLE_SCHEMA": "sample", "TABLE_NAME": "source", "INDEX_NAME": "PRIMARY",
                     "SEQ_IN_INDEX": 1, "COLUMN_NAME": "id"}]
        if "information_schema.views" in sql:
            return [{"TABLE_SCHEMA": "sample", "TABLE_NAME": "summary", "VIEW_DEFINITION": definition}]
        return []

    return Connection(respond)


def test_snapshot_mysql_rewritten_definition_enters_existing_column_lineage(monkeypatch):
    adapter, connection = connector(), snapshot_connection(REWRITTEN)
    monkeypatch.setattr(adapter, "_connect", lambda **kwargs: connection)
    snapshot = adapter.snapshot()
    table, view = snapshot.assets
    assert table.key_columns == ["id"] and table.unique_keys == [["id"]]
    assert table.physical_id is None and table.definition is None
    assert view.definition == REWRITTEN and view.kind == "view" and snapshot.warnings == []
    assert snapshot.server_identity["instance_id"] == "mysql-unit-uuid" and connection.closed
    schema = {"def": {"sample": {a.name: {c.name: c.data_type for c in a.columns} for a in snapshot.assets}}}
    parsed = parse_query(view.definition, dialect=MYSQL.sql, schema=schema, default_db=view.db)
    assert parsed.status == "ok", parsed.reason
    assert parsed.tables == ["def.sample.source"]
    total = next(c for c in parsed.columns if c.name == "total")
    assert {(s.ref.table, s.ref.column, s.relation) for s in total.sources} == {
        ("def.sample.source", "amount", "value_aggregated"),
        ("def.sample.source", "discount", "value_aggregated")}
    assert {(i.ref.column, i.relation) for i in parsed.influences} == {("id", "group_by"), ("active", "filter")}


def test_mysql_alias_join_and_quoted_cross_database_sources():
    sql = ("select `left_source`.`id` AS `id`,`right_source`.`value` AS `value` "
           "from (`sample`.`source` `left_source` join `other`.`reference` `right_source` "
           "on((`left_source`.`id` = `right_source`.`id`)))")
    schema = {"def": {"sample": {"source": {"id": "INT"}},
                      "other": {"reference": {"id": "INT", "value": "VARCHAR"}}}}
    parsed = parse_query(sql, dialect=MYSQL.sql, schema=schema)
    assert parsed.status == "ok", parsed.reason
    assert parsed.tables == ["def.other.reference", "def.sample.source"]
    assert parsed.columns[1].sources[0].ref.table == "def.other.reference"
    assert parsed.columns[1].sources[0].ref.column == "value"
    assert canonical_hash_of(sql, MYSQL.sql)[1] == canonical_hash_of(sql.replace("select ", "SELECT  "), MYSQL.sql)[1]


@pytest.mark.parametrize("definition", [None, ""])
def test_snapshot_missing_view_definition_is_an_explicit_warning(monkeypatch, definition):
    adapter, connection = connector(), snapshot_connection(definition)
    monkeypatch.setattr(adapter, "_connect", lambda **kwargs: connection)
    snapshot = adapter.snapshot()
    assert snapshot.assets[1].definition is None
    assert snapshot.warnings == ["MySQL 视图 sample.summary 未读取到定义（需要 SHOW VIEW 权限）；列级血缘不可用"]


def test_mysql_ddl_delegation_uses_bound_options(monkeypatch):
    adapter = connector({"ddl_audit_table": "mysql.general_log"})
    seen = []
    monkeypatch.setattr("avelis.connectors.mysql.audit_source", lambda options: "mysql:" + options["ddl_audit_table"])
    monkeypatch.setattr("avelis.connectors.mysql.ddl_events", lambda *args: seen.append(args) or [])
    since, until = datetime(2026, 1, 1), datetime(2026, 1, 2)
    assert adapter.ddl_audit_source == "mysql:mysql.general_log"
    assert adapter.ddl_events(since, until, cursors=(None, None)) == []
    assert seen == [(adapter, since, until, (None, None))]
