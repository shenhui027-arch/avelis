"""TiDB 适配器契约：引擎登记、会话语句、实例身份、错误归类与视图血缘；不连接外部数据库。

会话语句与视图定义原文取自 2026-11-28 在 TiDB v8.5.4 上的实测（见 connectors/tidb.py 模块说明）。
"""

from __future__ import annotations

import pymysql
import pytest

from avelis.analysis.explore import profile_for
from avelis.connectors.base import QueryError
from avelis.connectors.mysql import MySQLMetaConnector
from avelis.connectors.tidb import TIDB_MEMORY_QUOTA_ERRNO, TiDBConnector
from avelis.engines import get_engine
from avelis.engines.base import DDL_AUDIT, IDENTITY_TARGET, LIVE_CHECK, METADATA, QUERY, VIEW_LINEAGE
from avelis.governance.vqr import validate_vqr_sql
from avelis.lineage.sql import parse_query
from tests.unit.test_mysql_query_lineage import Connection


def connector(options=None):
    return TiDBConnector("unit-host", 4000, "read-user", "unit-password", options or {"databases": ["shop"]})


def test_engine_is_registered_with_mysql_dialect_and_honest_capabilities():
    engine = get_engine("tidb")
    assert engine.label == "TiDB"
    assert engine.default_port == 4000
    assert engine.sql.name == "mysql" and engine.sql.default_catalog == "def" and not engine.sql.catalog_in_sql
    assert engine.name_levels == ("database", "table")
    assert engine.capabilities == frozenset({METADATA, VIEW_LINEAGE, QUERY, LIVE_CHECK})
    assert not engine.supports(DDL_AUDIT) and not engine.supports(IDENTITY_TARGET)
    assert engine.collector is TiDBConnector and engine.querier is TiDBConnector
    assert issubclass(TiDBConnector, MySQLMetaConnector)
    assert {f.key for f in engine.options} == {"databases", "tls_mode", "tls_ca_cert"}


def test_explore_profile_and_vqr_use_mysql_dialect():
    profile = profile_for("tidb")
    assert profile is not None and profile.dialect == "mysql" and not profile.catalog_in_sql
    assert validate_vqr_sql("SELECT `a` FROM shop.orders LIMIT 5", "tidb")


def run_select(monkeypatch, *, mem_limit_bytes, respond=lambda sql, params: [(1,)], **conn_kwargs):
    conn = Connection(respond, **conn_kwargs)
    monkeypatch.setattr(TiDBConnector, "_connect", lambda self, read_timeout=60: conn)
    result = connector().execute_select(
        "SELECT 1", timeout_seconds=7, mem_limit_bytes=mem_limit_bytes, max_rows=100, timezone=None
    )
    return conn, result


def test_select_sets_limits_and_memory_quota_without_noop_read_only(monkeypatch):
    conn, result = run_select(monkeypatch, mem_limit_bytes=2 * 1024**3)
    statements = [sql for sql, _ in conn.statements]
    assert statements == [
        "SET SESSION MAX_EXECUTION_TIME = 7000",
        "SET SESSION SQL_SELECT_LIMIT = 101",
        f"SET SESSION tidb_mem_quota_query = {2 * 1024**3}",
        "SELECT 1",
    ]
    # TiDB 的 READ ONLY 只有空实现（报 1235）；历史读事务读不到刚变更的表结构：两种都不用
    assert not any("TRANSACTION" in sql for sql in statements)
    assert conn.rollbacks == 1 and conn.closed
    assert result.rows == [[1]] and not result.truncated


def test_zero_memory_limit_keeps_server_default(monkeypatch):
    conn, _ = run_select(monkeypatch, mem_limit_bytes=0)
    assert not any("tidb_mem_quota_query" in sql for sql, _ in conn.statements)


def test_mysql_connector_behaviour_is_unchanged(monkeypatch):
    conn = Connection(lambda sql, params: [(1,)])
    monkeypatch.setattr(MySQLMetaConnector, "_connect", lambda self, read_timeout=60: conn)
    MySQLMetaConnector("h", 3306, "u", "p", {"databases": ["x"]}).execute_select(
        "SELECT 1", timeout_seconds=7, mem_limit_bytes=2 * 1024**3, max_rows=100, timezone=None
    )
    assert [sql for sql, _ in conn.statements] == [
        "SET SESSION MAX_EXECUTION_TIME = 7000",
        "SET SESSION SQL_SELECT_LIMIT = 101",
        "START TRANSACTION READ ONLY",
        "SELECT 1",
    ]


@pytest.mark.parametrize(
    ("errno", "message", "code"),
    [
        (TIDB_MEMORY_QUOTA_ERRNO, "Your query has been cancelled due to exceeding the allowed memory limit", "other"),
        (8121, "privilege check for 'Update' fail", "permission"),
        (3024, "Query execution was interrupted, maximum statement execution time exceeded", "timeout"),
        (1142, "SELECT command denied to user 'ro'@'%' for table 'orders'", "permission"),
        (1146, "Table 'shop.nope' doesn't exist", "invalid_object"),
    ],
)
def test_errors_are_classified_and_keep_original_text(monkeypatch, errno, message, code):
    error = pymysql.err.OperationalError(errno, message)
    with pytest.raises(QueryError) as caught:
        run_select(monkeypatch, mem_limit_bytes=0, fail_at="SELECT 1", error=error)
    assert caught.value.code == code and caught.value.db_errno == errno and caught.value.message == message


def test_identity_has_no_instance_id_and_ignores_server_uuid():
    seen = []

    def respond(sql, params):
        seen.append(sql)
        return [{"hostname": "tidb-0", "port": 4000, "version": "8.0.11-TiDB-v8.5.4"}]

    identity = TiDBConnector._server_identity(Connection(respond).cursor())
    assert identity == {"instance_id": None, "hostname": "tidb-0", "port": 4000, "version": "8.0.11-TiDB-v8.5.4"}
    assert not any("server_uuid" in sql for sql in seen)


def test_physical_id_is_not_taken_from_tidb_table_id():
    assert connector()._physical_id({"TIDB_TABLE_ID": 112, "TABLE_NAME": "orders"}) is None


def test_ddl_attribution_is_reported_as_unsupported_with_tidb_reason():
    c = connector({"databases": ["shop"], "ddl_audit_table": "mysql.general_log"})
    assert c.ddl_audit_source is None
    assert "TiDB" in c.ddl_audit_unbound_reason
    assert "TiDB" in c._missing_view_definition("shop.v")


def test_stored_view_definition_parses_to_column_lineage():
    engine = get_engine("tidb")
    definition = (
        "SELECT `o`.`user_id` AS `user_id`,SUM(`o`.`amt`) AS `total` FROM `shop`.`orders` AS `o` "
        "WHERE `o`.`amt`>0 GROUP BY `o`.`user_id`"
    )
    schema = {"def": {"shop": {"orders": {"id": "bigint", "user_id": "int", "amt": "decimal(10,2)"}}}}
    parsed = parse_query(definition, dialect=engine.sql, schema=schema, default_db="shop")
    assert parsed.status == "ok", parsed.reason
    assert parsed.tables == ["def.shop.orders"]
    total = next(c for c in parsed.columns if c.name == "total")
    assert {(s.ref.table, s.ref.column, s.relation) for s in total.sources} == {
        ("def.shop.orders", "amt", "value_aggregated")
    }
    assert {(i.ref.column, i.relation) for i in parsed.influences} == {("user_id", "group_by"), ("amt", "filter")}
