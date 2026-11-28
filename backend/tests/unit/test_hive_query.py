"""Hive 问数执行、执行前核对与数据新鲜度的契约；不连接外部数据库。

行为依据 2026-11-28 在 Hive 4.1 + Tez 上的实测（见 connectors/hive.py 模块说明“问数执行”）。
"""

from __future__ import annotations

import pytest
from impala.error import HiveServer2Error

from avelis.connectors import hive as hive_module
from avelis.connectors.base import QueryError
from avelis.connectors.hive import BACKSTOP_SECONDS, HiveConnector, partition_spec_sql
from avelis.engines import get_engine
from avelis.engines.base import DDL_AUDIT, LIVE_CHECK, METADATA, QUERY, VIEW_LINEAGE
from tests.unit.test_hive import SALES, SHOPS, SHOW_CREATE_VIEW, VIEW

TABLE_NOT_FOUND = (
    "Error while compiling statement: FAILED: SemanticException [Error 10001]: Line 1:14 Table not found 'nope'"
)


class AsyncCursor:
    """脚本化的 impyla 游标：execute_async 之后 is_executing 依次返回 running 里的值。"""

    def __init__(
        self, *, running=(False,), rows=(), description=(("total",),), fail=None, responses=None, finish_error=None
    ):
        self.finish_error = finish_error
        self.running = list(running)
        self.rows = list(rows)
        self.description = list(description)
        self.fail = fail
        self.responses = responses or {}
        self.async_calls: list[tuple[str, dict]] = []
        self.executed: list[str] = []
        self.cancelled = self.closed = False
        self.fetch_counts: list[int] = []
        self._result: list[tuple] = []

    def execute_async(self, sql, configuration=None):
        self.async_calls.append((sql, dict(configuration or {})))

    def is_executing(self):
        if self.fail is not None:
            raise self.fail
        return self.running.pop(0) if self.running else False

    def cancel_operation(self):
        self.cancelled = True

    def _wait_to_finish(self):
        if self.finish_error is not None:
            raise self.finish_error

    def fetchmany(self, count):
        self.fetch_counts.append(count)
        return self.rows[:count]

    def execute(self, sql, configuration=None):
        self.executed.append(sql)
        result = self.responses.get(sql)
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise AssertionError(f"未预期的语句：{sql}")
        self._result = list(result)

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None

    def close(self):
        self.closed = True


class Connection:
    def __init__(self, cursor):
        self._cursor, self.closed, self.timeouts, self.users = cursor, False, [], []

    def cursor(self, user=None):
        self.users.append(user)
        return self._cursor

    def close(self):
        self.closed = True


def connector(monkeypatch, cursor, options=None):
    conn = Connection(cursor)
    c = HiveConnector("hs2", 10000, "query", "", options or {"databases": ["store"]})

    def connect(timeout=60):
        conn.timeouts.append(timeout)
        return conn

    monkeypatch.setattr(c, "_connect", connect)
    return c, conn


def test_engine_declares_query_and_live_check_but_not_ddl_audit():
    engine = get_engine("hive")
    assert engine.capabilities == frozenset({METADATA, VIEW_LINEAGE, QUERY, LIVE_CHECK})
    assert not engine.supports(DDL_AUDIT)
    assert engine.querier is HiveConnector and engine.collector is HiveConnector


def test_select_runs_async_with_backstop_timeout_timezone_and_row_cap(monkeypatch):
    cursor = AsyncCursor(running=[True, True, False], rows=[(1,), (2,), (3,)])
    monkeypatch.setattr(hive_module.time, "sleep", lambda s: None)
    c, conn = connector(monkeypatch, cursor)
    result = c.execute_select(
        "SELECT 1", timeout_seconds=20, mem_limit_bytes=2 * 1024**3, max_rows=2, timezone="Asia/Shanghai"
    )
    assert cursor.async_calls == [
        (
            "SELECT 1",
            {
                "hive.query.timeout.seconds": str(20 + BACKSTOP_SECONDS),
                "hive.vectorized.execution.enabled": "false",
                "hive.local.time.zone": "Asia/Shanghai",
            },
        )
    ]
    assert conn.timeouts == [20 + BACKSTOP_SECONDS] and cursor.fetch_counts == [3]
    assert result.columns == ["total"] and result.rows == [[1], [2]] and result.truncated
    assert not cursor.cancelled and cursor.closed and conn.closed
    # Hive 没有每查询内存上限：不出现任何内存相关参数
    assert not any("mem" in key for key in cursor.async_calls[0][1])


def test_no_timezone_means_no_timezone_setting(monkeypatch):
    cursor = AsyncCursor()
    c, _ = connector(monkeypatch, cursor)
    c.execute_select("SELECT 1", timeout_seconds=5, mem_limit_bytes=0, max_rows=10)
    assert cursor.async_calls[0][1] == {
        "hive.query.timeout.seconds": str(5 + BACKSTOP_SECONDS),
        "hive.vectorized.execution.enabled": "false",
    }


def test_deadline_cancels_the_operation(monkeypatch):
    clock = iter([0.0, 0.0, 1.0, 2.0, 3.5, 4.0, 5.0])
    monkeypatch.setattr(hive_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(hive_module.time, "sleep", lambda s: None)
    cursor = AsyncCursor(running=[True] * 10)
    c, conn = connector(monkeypatch, cursor)
    with pytest.raises(QueryError) as caught:
        c.execute_select("SELECT 1", timeout_seconds=3, mem_limit_bytes=0, max_rows=10)
    assert caught.value.code == "timeout" and "3" in caught.value.message
    assert cursor.cancelled and cursor.closed and conn.closed and cursor.fetch_counts == []


def test_invalid_arguments_are_rejected_before_connecting(monkeypatch):
    cursor = AsyncCursor()
    c, conn = connector(monkeypatch, cursor)
    with pytest.raises(QueryError) as caught:
        c.execute_select("SELECT 1", timeout_seconds=0, mem_limit_bytes=0, max_rows=10)
    assert caught.value.code == "timeout"
    with pytest.raises(QueryError) as caught:
        c.execute_select("SELECT 1", timeout_seconds=5, mem_limit_bytes=0, max_rows=-1)
    assert caught.value.code == "other"
    with pytest.raises(QueryError, match="Mars/Olympus"):
        c.execute_select("SELECT 1", timeout_seconds=5, mem_limit_bytes=0, max_rows=10, timezone="Mars/Olympus")
    assert conn.timeouts == [] and cursor.async_calls == []


@pytest.mark.parametrize(
    ("message", "code", "errno"),
    [
        (TABLE_NOT_FOUND + "; Query ID: hive_1", "invalid_object", 10001),
        (
            "FAILED: SemanticException [Error 10004]: Line 1:7 Invalid table alias or column reference 'x'",
            "invalid_object",
            10004,
        ),
        (
            "FAILED: HiveAccessControlException Permission denied: user [ro] does not have [UPDATE] privilege",
            "permission",
            None,
        ),
        ("Operation timed out", "timeout", None),
        ("FAILED: Execution Error, return code 2 from org.apache.hadoop.hive.ql.exec.tez.TezTask", "other", None),
    ],
)
def test_errors_are_classified_and_keep_original_text(monkeypatch, message, code, errno):
    cursor = AsyncCursor(fail=HiveServer2Error(message))
    c, conn = connector(monkeypatch, cursor)
    with pytest.raises(QueryError) as caught:
        c.execute_select("SELECT 1", timeout_seconds=5, mem_limit_bytes=0, max_rows=10)
    assert caught.value.code == code and caught.value.db_errno == errno
    assert "Query ID" not in caught.value.message and caught.value.message.startswith(
        message.split("; Query ID")[0][:40]
    )
    assert cursor.closed and conn.closed


def test_describe_objects_matches_collection_and_handles_missing_objects(monkeypatch):
    responses = {
        "DESCRIBE FORMATTED `store`.`sales`": SALES,
        "DESCRIBE FORMATTED `store`.`city_daily_sales`": VIEW,
        "SHOW CREATE TABLE `store`.`city_daily_sales`": [(line,) for line in SHOW_CREATE_VIEW.split("\n")],
        "DESCRIBE FORMATTED `store`.`nope`": HiveServer2Error(TABLE_NOT_FOUND),
    }
    cursor = AsyncCursor(responses=responses)
    c, conn = connector(monkeypatch, cursor)
    out = c.describe_objects(
        [("store", "sales"), ("STORE", "City_Daily_Sales"), ("store", "nope"), ("store", "x; DROP")], timeout_seconds=9
    )
    assert out[("store", "sales")]["columns"] == [
        ["sale_id", "bigint"],
        ["shop_id", "bigint"],
        ["status", "varchar(16)"],
        ["amount", "decimal(18,2)"],
        ["sale_date", "date"],
    ]
    assert out[("store", "sales")]["definition"] is None and out[("store", "sales")]["physical_id"] is None
    view = out[("STORE", "City_Daily_Sales")]
    assert view["definition"].startswith("SELECT `s`.`sale_date`") and view["columns"][0] == ["sale_date", "date"]
    assert out[("store", "nope")] == {"columns": [], "definition": None, "physical_id": None}
    assert out[("store", "x; DROP")] == {"columns": [], "definition": None, "physical_id": None}
    assert not any("x; DROP" in sql for sql in cursor.executed) and conn.timeouts == [9] and conn.closed


def test_describe_objects_reports_other_errors(monkeypatch):
    cursor = AsyncCursor(responses={"DESCRIBE FORMATTED `store`.`sales`": HiveServer2Error("Permission denied: ro")})
    c, _ = connector(monkeypatch, cursor)
    with pytest.raises(QueryError) as caught:
        c.describe_objects([("store", "sales")])
    assert caught.value.code == "permission"


def _with_ddl_time(rows, seconds):
    out = list(rows)
    at = next(i for i, r in enumerate(out) if r[0] == "Table Parameters:")
    out.insert(at + 1, ("", "transient_lastDdlTime", f"{seconds}          "))
    return out


PARTITION = [
    ("sale_id", "bigint", ""),
    ("", None, None),
    ("# Detailed Partition Information", None, None),
    ("Partition Value:    ", "[2026-11-02]        ", None),
    ("Partition Parameters:", None, None),
    ("", "numRows             ", "1                   "),
    ("", "transient_lastDdlTime", "1795856864          "),
    ("", None, None),
]


def test_freshness_uses_latest_partition_or_table_ddl_time(monkeypatch):
    responses = {
        "DESCRIBE FORMATTED `store`.`sales`": SALES,
        "SHOW PARTITIONS `store`.`sales` ORDER BY `sale_date` DESC LIMIT 1": [("sale_date=2026-11-02",)],
        "DESCRIBE FORMATTED `store`.`sales` PARTITION (`sale_date`='2026-11-02')": PARTITION,
        "DESCRIBE FORMATTED `store`.`shops`": _with_ddl_time(SHOPS, 1795800000),
        "DESCRIBE FORMATTED `store`.`gone`": HiveServer2Error(TABLE_NOT_FOUND),
    }
    cursor = AsyncCursor(responses=responses)
    c, conn = connector(monkeypatch, cursor)
    out = c.freshness([("store", "sales"), ("store", "shops"), ("store", "gone"), ("store", "bad name")])
    assert out == {
        "store.sales": "2026-11-28T09:07:44+00:00",
        "store.shops": "2026-11-27T17:20:00+00:00",
        "store.gone": None,
    }
    assert conn.closed


def test_freshness_without_partitions_or_tables(monkeypatch):
    responses = {
        "DESCRIBE FORMATTED `store`.`sales`": SALES,
        "SHOW PARTITIONS `store`.`sales` ORDER BY `sale_date` DESC LIMIT 1": [],
    }
    cursor = AsyncCursor(responses=responses)
    c, conn = connector(monkeypatch, cursor)
    assert c.freshness([("store", "sales")]) == {"store.sales": None}
    c2, conn2 = connector(monkeypatch, AsyncCursor())
    assert c2.freshness([]) == {} and conn2.timeouts == []


@pytest.mark.parametrize(
    ("spec", "sql"),
    [
        ("dt=2026-11-02", "`dt`='2026-11-02'"),
        ("a=1/b=x", "`a`='1', `b`='x'"),
        ("dt=2026-11-02 10%3A00%3A00", "`dt`='2026-11-02 10:00:00'"),
        ("k=o'k", "`k`='o\\'k'"),
        ("k=a\\b", "`k`='a\\\\b'"),
        ("k=x=y", "`k`='x=y'"),
    ],
)
def test_partition_spec_sql(spec, sql):
    assert partition_spec_sql(spec) == sql


def test_partition_spec_sql_rejects_bad_column_names():
    with pytest.raises(ValueError):
        partition_spec_sql("a b=1")


def test_runtime_failure_keeps_the_database_error(monkeypatch):
    # 编译通过、后台执行失败：is_executing() 只返回 False，原始报错要从终态里取回，不能被取结果元数据的报错盖掉
    failure = HiveServer2Error("FAILED: Execution Error, return code 2 from org.apache.hadoop.hive.ql.exec.tez.TezTask")
    cursor = AsyncCursor(running=[False], finish_error=failure)
    c, _ = connector(monkeypatch, cursor)
    with pytest.raises(QueryError) as caught:
        c.execute_select("SELECT 1", timeout_seconds=5, mem_limit_bytes=0, max_rows=10)
    assert "TezTask" in caught.value.message and cursor.fetch_counts == []


def test_sessions_open_as_the_configured_user(monkeypatch):
    cursor = AsyncCursor(responses={"DESCRIBE FORMATTED `store`.`sales`": SALES})
    conn = Connection(cursor)
    c = HiveConnector("hs2", 10000, "", "", {"databases": ["store"], "auth": "NOSASL"})
    monkeypatch.setattr(c, "_connect", lambda timeout=60: conn)
    c.execute_select("SELECT 1", timeout_seconds=5, mem_limit_bytes=0, max_rows=10)
    c.describe_objects([("store", "sales")])
    assert conn.users == ["avelis", "avelis"]
    ldap = HiveConnector("hs2", 10000, "ro_user", "pw", {"databases": ["store"], "auth": "LDAP"})
    monkeypatch.setattr(ldap, "_connect", lambda timeout=60: conn)
    ldap.execute_select("SELECT 1", timeout_seconds=5, mem_limit_bytes=0, max_rows=10)
    assert conn.users[-1] == "ro_user"
