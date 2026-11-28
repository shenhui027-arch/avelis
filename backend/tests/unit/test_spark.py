"""Spark SQL 适配器契约：DESCRIBE FORMATTED 解析、视图定义与解析上下文、会话参数、分区新鲜度与错误归类；不连库。

输出样例取自 2026-11-28 在 Spark 4.0.1 Thrift Server 上的实测（见 connectors/spark.py 模块说明）。
"""

from __future__ import annotations

import pytest
from impala.error import OperationalError

from avelis.analysis.explore import profile_for
from avelis.connectors.base import QueryError
from avelis.connectors.hive import BACKSTOP_SECONDS, HiveConnector
from avelis.connectors.spark import (
    CATALOG,
    SparkConnector,
    bracket_properties,
    parse_spark_described,
    spark_view_definition,
)
from avelis.engines import get_engine
from avelis.engines.base import DDL_AUDIT, LIVE_CHECK, METADATA, QUERY, VIEW_LINEAGE
from avelis.lineage.sql import parse_query
from tests.unit.test_hive_query import AsyncCursor, Connection

TABLE = [
    ("id", "int", "ID"),
    ("city", "string", None),
    ("amt", "decimal(10,2)", "金额"),
    ("tags", "array<string>", None),
    ("dt", "string", None),
    ("# Partition Information", "", ""),
    ("# col_name", "data_type", "comment"),
    ("dt", "string", None),
    ("", "", ""),
    ("# Detailed Table Information", "", ""),
    ("Catalog", "spark_catalog", ""),
    ("Database", "probe", ""),
    ("Table", "t", ""),
    ("Owner", "avelis", ""),
    ("Created Time", "Sat Nov 28 10:37:53 UTC 2026", ""),
    ("Last Access", "UNKNOWN", ""),
    ("Created By", "Spark 4.0.1", ""),
    ("Type", "MANAGED", ""),
    ("Provider", "parquet", ""),
    ("Comment", "表注释", ""),
    ("Table Properties", "[transient_lastDdlTime=1795862273]", ""),
    ("Location", "file:/opt/spark/work-dir/warehouse/probe.db/t", ""),
    ("Partition Provider", "Catalog", ""),
]

VIEW = [
    ("city", "string", None),
    ("total", "decimal(20,2)", None),
    ("", "", ""),
    ("# Detailed Table Information", "", ""),
    ("Catalog", "spark_catalog", ""),
    ("Database", "probe", ""),
    ("Table", "v", ""),
    ("Type", "VIEW", ""),
    ("Comment", "视图", ""),
    ("View Text", "SELECT city, SUM(amt) AS total FROM t WHERE amt > 0 GROUP BY city", ""),
    ("View Original Text", "SELECT city, SUM(amt) AS total FROM t WHERE amt > 0 GROUP BY city", ""),
    ("View Schema Mode", "COMPENSATION", ""),
    ("View Catalog and Namespace", "spark_catalog.probe", ""),
    ("View Query Output Columns", "[`city`, `total`]", ""),
    ("Table Properties", "[transient_lastDdlTime=1795862276]", ""),
]

# View Text 按建视图时的当前库（spark_catalog.probe）补全后的定义
COMPLETED = "SELECT city, SUM(amt) AS total FROM probe.t AS t WHERE amt > 0 GROUP BY city"

PARTITION = [
    ("id", "int", "ID"),
    ("dt", "string", None),
    ("# Partition Information", "", ""),
    ("# col_name", "data_type", "comment"),
    ("dt", "string", None),
    ("", "", ""),
    ("# Detailed Partition Information", "", ""),
    ("Database", "probe", ""),
    ("Table", "t", ""),
    ("Partition Values", "[dt=2026-11-02]", ""),
    ("Partition Parameters", "[transient_lastDdlTime=1795856864, totalSize=2639, numFiles=2]", ""),
    ("Created Time", "Sat Nov 28 10:37:55 UTC 2026", ""),
    ("", "", ""),
    ("# Storage Information", "", ""),
    ("Location", "file:/opt/spark/work-dir/warehouse/probe.db/t", ""),
]


def connector(monkeypatch, cursor, options=None):
    conn = Connection(cursor)
    c = SparkConnector("sts", 10000, "", "", options or {"databases": ["probe"]})

    def connect(timeout=60):
        conn.timeouts.append(timeout)
        return conn

    monkeypatch.setattr(c, "_connect", connect)
    return c, conn


def test_engine_registration():
    engine = get_engine("spark")
    assert engine.label == "Spark SQL" and engine.default_port == 10000
    assert engine.capabilities == frozenset({METADATA, VIEW_LINEAGE, QUERY, LIVE_CHECK}) and not engine.supports(
        DDL_AUDIT
    )
    assert engine.sql.name == "spark" and engine.sql.default_catalog == CATALOG and not engine.sql.catalog_in_sql
    assert engine.collector is SparkConnector and engine.querier is SparkConnector
    assert issubclass(SparkConnector, HiveConnector) and not engine.credentials_required({})
    profile = profile_for("spark")
    assert profile is not None and profile.dialect == "spark" and not profile.catalog_in_sql


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[transient_lastDdlTime=1795862276]", {"transient_lastDdlTime": "1795862276"}),
        ("[a=1, b=x=y, c]", {"a": "1", "b": "x=y"}),
        ("  [ ]  ", {}),
        ("transient_lastDdlTime=1", {}),
        (None, {}),
        ("", {}),
    ],
)
def test_bracket_properties(text, expected):
    assert bracket_properties(text) == expected


def test_parse_table_drops_duplicated_partition_columns_and_maps_type():
    d = parse_spark_described(TABLE)
    assert d.columns == [
        ("id", "int", "ID"),
        ("city", "string", None),
        ("amt", "decimal(10,2)", "金额"),
        ("tags", "array<string>", None),
    ]
    assert d.partition_columns == [("dt", "string", None)]
    assert d.table_type == "MANAGED_TABLE" and d.comment == "表注释"
    assert d.parameters["transient_lastDdlTime"] == "1795862273"
    assert d.info["Provider"] == "parquet" and d.info["CreateTime"] == "Sat Nov 28 10:37:53 UTC 2026"
    assert d.expanded_query is None


def test_parse_view_keeps_original_text_and_namespace():
    d = parse_spark_described(VIEW)
    assert d.table_type == "VIRTUAL_VIEW" and d.comment == "视图"
    assert d.expanded_query == "SELECT city, SUM(amt) AS total FROM t WHERE amt > 0 GROUP BY city"
    assert d.info["View Catalog and Namespace"] == "spark_catalog.probe"
    assert d.partition_columns == [] and [c[0] for c in d.columns] == ["city", "total"]


def test_parse_partition_parameters():
    d = parse_spark_described(PARTITION)
    assert d.parameters["transient_lastDdlTime"] == "1795856864" and d.parameters["numFiles"] == "2"


def test_snapshot_records_view_context_and_skips_show_create(monkeypatch):
    responses = {
        "SELECT version()": [("4.0.1 29434ea766b0fc3c3bf6eaadb43a8f931133649e",)],
        "SHOW DATABASES": [("default",), ("probe",)],
        "SHOW TABLES IN `probe`": [("probe", "t", False), ("probe", "v", False)],
        "DESCRIBE FORMATTED `probe`.`t`": TABLE,
        "DESCRIBE FORMATTED `probe`.`v`": VIEW,
    }
    cursor = AsyncCursor(responses=responses)
    c, conn = connector(monkeypatch, cursor)
    snap = c.snapshot()
    assets = {a.qualified: a for a in snap.assets}
    assert set(assets) == {"spark_catalog.probe.t", "spark_catalog.probe.v"} and snap.warnings == []
    table, view = assets["spark_catalog.probe.t"], assets["spark_catalog.probe.v"]
    assert [col.name for col in table.columns] == ["id", "city", "amt", "tags", "dt"]
    assert (
        table.kind == "table" and table.properties["provider"] == "parquet" and "view_context" not in table.properties
    )
    assert view.kind == "view" and view.definition == COMPLETED
    assert view.properties["view_context"] == {"catalog": "spark_catalog", "db": "probe"}
    assert not any(sql.startswith("SHOW CREATE") for sql in cursor.executed) and conn.closed


def test_select_sets_spark_session_with_statements(monkeypatch):
    cursor = AsyncCursor(rows=[(1,), (2,)], responses={})
    executed = []
    cursor.execute = lambda sql, configuration=None: executed.append(sql)
    c, _ = connector(monkeypatch, cursor)
    result = c.execute_select("SELECT 1", timeout_seconds=20, mem_limit_bytes=0, max_rows=1, timezone="Asia/Shanghai")
    assert executed == [
        f"SET spark.sql.thriftServer.queryTimeout={20 + BACKSTOP_SECONDS}s",
        "SET spark.sql.session.timeZone=Asia/Shanghai",
    ]
    assert cursor.async_calls == [("SELECT 1", {})] and result.rows == [[1]] and result.truncated


def test_select_without_timezone_sets_only_timeout(monkeypatch):
    cursor = AsyncCursor(rows=[(1,)], responses={})
    executed = []
    cursor.execute = lambda sql, configuration=None: executed.append(sql)
    c, _ = connector(monkeypatch, cursor)
    c.execute_select("SELECT 1", timeout_seconds=5, mem_limit_bytes=0, max_rows=10)
    assert executed == [f"SET spark.sql.thriftServer.queryTimeout={5 + BACKSTOP_SECONDS}s"]


def test_freshness_uses_largest_partition_spec(monkeypatch):
    responses = {
        "DESCRIBE FORMATTED `probe`.`t`": TABLE,
        "SHOW PARTITIONS `probe`.`t`": [("dt=2026-11-01",), ("dt=2026-11-02",)],
        "DESCRIBE FORMATTED `probe`.`t` PARTITION (`dt`='2026-11-02')": PARTITION,
        "DESCRIBE FORMATTED `probe`.`v`": VIEW,
    }
    c, _ = connector(monkeypatch, AsyncCursor(responses=responses))
    assert c.freshness([("probe", "t"), ("probe", "v")]) == {
        "probe.t": "2026-11-28T09:07:44+00:00",
        "probe.v": "2026-11-28T10:37:56+00:00",
    }


SPARK_PREFIX = "org.apache.hive.service.cli.HiveSQLException: Error running query: "


@pytest.mark.parametrize(
    ("message", "code", "errno"),
    [
        (
            SPARK_PREFIX
            + "[TABLE_OR_VIEW_NOT_FOUND] org.apache.spark.sql.AnalysisException: [TABLE_OR_VIEW_NOT_FOUND] "
            "The table or view `probe`.`nope` cannot be found.",
            "invalid_object",
            "TABLE_OR_VIEW_NOT_FOUND",
        ),
        (
            SPARK_PREFIX + "[UNRESOLVED_COLUMN.WITH_SUGGESTION] A column, variable, or function parameter with name "
            "`nope` cannot be resolved.",
            "invalid_object",
            "UNRESOLVED_COLUMN.WITH_SUGGESTION",
        ),
        (
            SPARK_PREFIX + "org.apache.hadoop.security.AccessControlException: Permission denied: user=ro",
            "permission",
            None,
        ),
        (SPARK_PREFIX + "[DIVIDE_BY_ZERO] Division by zero.", "other", "DIVIDE_BY_ZERO"),
    ],
)
def test_errors_are_classified_by_spark_error_class(monkeypatch, message, code, errno):
    cursor = AsyncCursor(fail=OperationalError(message))
    cursor.execute = lambda sql, configuration=None: None  # 会话 SET 语句
    c, _ = connector(monkeypatch, cursor)
    with pytest.raises(QueryError) as caught:
        c.execute_select("SELECT 1", timeout_seconds=5, mem_limit_bytes=0, max_rows=10)
    assert caught.value.code == code and caught.value.db_errno == errno


def test_describe_objects_treats_missing_tables_as_empty(monkeypatch):
    missing = OperationalError(
        SPARK_PREFIX + "[TABLE_OR_VIEW_NOT_FOUND] The table or view `probe`.`gone` cannot be found."
    )
    responses = {"DESCRIBE FORMATTED `probe`.`v`": VIEW, "DESCRIBE FORMATTED `probe`.`gone`": missing}
    c, _ = connector(monkeypatch, AsyncCursor(responses=responses))
    out = c.describe_objects([("probe", "v"), ("probe", "gone")])
    assert out[("probe", "v")]["definition"] == COMPLETED and out[("probe", "v")]["columns"][0] == ["city", "string"]
    assert out[("probe", "gone")] == {"columns": [], "definition": None, "physical_id": None}


def test_view_text_parses_with_view_context_database():
    dialect = get_engine("spark").sql
    schema = {CATALOG: {"probe": {"t": {"city": "string", "amt": "decimal(10,2)", "dt": "string"}}}}
    parsed = parse_query(VIEW[9][1], dialect=dialect, schema=schema, default_db="probe")
    assert parsed.status == "ok", parsed.reason
    assert parsed.tables == ["spark_catalog.probe.t"]
    total = next(col for col in parsed.columns if col.name == "total")
    assert {(s.ref.column, s.relation) for s in total.sources} == {("amt", "value_aggregated")}


def test_view_definition_is_completed_with_namespace_only():
    # 不写库名的表按建视图时的当前库补全；CTE 不动；查询的其余部分（别名、ORDER BY）原样保留
    assert spark_view_definition("SELECT id AS original_id FROM t ORDER BY original_id", "spark_catalog.reporting") == (
        "SELECT id AS original_id FROM reporting.t AS t ORDER BY original_id"
    )
    assert spark_view_definition("WITH c AS (SELECT id FROM t) SELECT id FROM c", "spark_catalog.x") == (
        "WITH c AS (SELECT id FROM x.t AS t) SELECT id FROM c AS c"
    )
    assert spark_view_definition("SELECT * FROM t", "spark_catalog.d") == "SELECT * FROM d.t AS t"
    assert spark_view_definition("NOT SQL ((", "spark_catalog.d") == "NOT SQL (("


def test_same_text_under_another_current_database_changes_the_definition():
    a = spark_view_definition("SELECT id FROM t", "spark_catalog.sales")
    b = spark_view_definition("SELECT id FROM t", "spark_catalog.archive")
    assert a != b  # 定义的指纹与执行前核对都会看到建视图时的当前库变化


def test_uppercase_tables_resolve_with_spark_folding():
    dialect = get_engine("spark").sql
    schema = {CATALOG: {"probe": {"sales": {"id": "int", "amt": "decimal(10,2)"}}}}
    sql = spark_view_definition("SELECT ID, SUM(AMT) FROM SALES GROUP BY ID", "spark_catalog.probe")
    parsed = parse_query(sql, dialect=dialect, schema=schema, default_db="probe")
    assert parsed.status == "ok", parsed.reason
    assert parsed.tables == ["spark_catalog.probe.sales"]
    assert parsed.group_outputs  # 分组仍在顶层：粒度可推导
