"""过拟合第 2 批：时间语义的离线回归。"""

import importlib.util
import json
from contextlib import contextmanager as conn_contextmanager
from datetime import date as svc_date
from pathlib import Path
from types import SimpleNamespace
from types import SimpleNamespace as conn_SimpleNamespace
from types import SimpleNamespace as svc_ns

import pymysql as conn_pymysql
import pytest
import pytest as conn_pytest
import pytest as svc_pytest
import sqlglot as svc_sqlglot
from alembic.config import Config
from alembic.script import ScriptDirectory

from avelis.ask import service as svc_service
from avelis.ask.intent import Intent as SvcIntent
from avelis.connectors.base import QueryError as conn_QueryError
from avelis.connectors.base import QueryResult as SvcResult
from avelis.connectors.base import current_timezone_offset as conn_current_offset
from avelis.connectors.clickhouse import ClickHouseConnector as conn_ClickHouseConnector
from avelis.connectors.doris import DorisConnector as conn_DorisConnector
from avelis.connectors.fake import FakeConnector as conn_FakeConnector
from avelis.connectors.mysql import MySQLMetaConnector as conn_MySQLConnector
from avelis.connectors.oracle import OracleConnector as conn_OracleConnector
from avelis.connectors.postgres import PostgresConnector as conn_PostgresConnector
from avelis.connectors.sqlserver import SqlServerConnector as conn_SqlServerConnector
from avelis.connectors.starrocks import StarRocksConnector as conn_StarRocksConnector
from avelis.governance import metrics as meta_metrics
from avelis.governance.errors import Invalid as MetaInvalid
from avelis.lineage.sql import SqlDialect as SvcDialect
from avelis.models import Answer as SvcAnswer
from avelis.models import Datasource as SvcDatasource
from avelis.models import Metric as SvcMetric
from avelis.models import MetricVersion as MetaMetricVersion
from avelis.models import MetricVersion as SvcVersion


def meta_version(**kwargs):
    values = {"kind": "base", "expression": 'SUM(b."指标甲")', "time_column": 'b."时间甲"',
              "timezone": "UTC", "dimensions": [], "joins": [], "filters": None}
    return MetaMetricVersion(**(values | kwargs))


def meta_validate(monkeypatch, *, data_type="date", **kwargs):
    asset = SimpleNamespace(id="资产甲", datasource_id="数据源甲", catalog_name="目录甲", db_name="库甲", name="表甲")
    monkeypatch.setattr(meta_metrics, "resolve_joins", lambda *args: ({"b": asset}, []))
    monkeypatch.setattr(meta_metrics, "_asset_columns", lambda *args: {
        "时间甲": SimpleNamespace(data_type=data_type), "指标甲": SimpleNamespace(data_type="numeric")})
    monkeypatch.setattr(meta_metrics, "engine_of", lambda *args: SimpleNamespace(sql=SimpleNamespace(name="postgres")))
    monkeypatch.setattr(meta_metrics, "current_fingerprints", lambda *args: {})
    return meta_metrics.validate_spec(SimpleNamespace(get=lambda *args: object()), meta_version(**kwargs))


def test_metric_time_migration_and_model_defaults(monkeypatch):
    path = Path(__file__).parents[2] / "src/avelis/migrations/versions/0017_metric_time_semantics.py"
    spec = importlib.util.spec_from_file_location("metric_time_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    columns, dropped = [], []
    monkeypatch.setattr(migration.op, "add_column", lambda table, column: columns.append((table, column)))
    monkeypatch.setattr(migration.op, "drop_column", lambda table, column: dropped.append((table, column)))
    migration.upgrade()
    assert migration.revision == "0017_metric_time" and migration.down_revision == "0016_conversation"
    assert all(table == "metric_version" for table, _ in columns)
    assert [column.name for _, column in columns] == [
        "time_format", "data_grain", "time_aggregation", "value_kind", "polarity"]
    for _, column in columns:
        model_column = MetaMetricVersion.__table__.c[column.name]
        assert column.type.length == model_column.type.length
        assert column.nullable == model_column.nullable
        if column.name in ("data_grain", "time_aggregation"):
            expected = "day" if column.name == "data_grain" else "sum"
            assert column.server_default.arg == model_column.server_default.arg == expected
        else:
            assert column.nullable
    migration.downgrade()
    assert [name for _, name in dropped] == [column.name for _, column in reversed(columns)]


def test_metric_time_migration_is_the_single_head_after_conversations():
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["0017_metric_time"]
    assert scripts.get_revision("0017_metric_time").down_revision == "0016_conversation"
    assert scripts.get_revision("0016_conversation").down_revision == "0015"


def test_metric_time_input_defaults_and_storage_fields():
    from avelis.api.governance import VersionSpec
    from avelis.api.schemas import MetricVersionOut

    spec = VersionSpec(description="指标甲", base_asset_id="资产甲", expression="SUM(b.value_a)", time_column="b.time_a")
    assert {key: spec.as_spec()[key] for key in meta_metrics.TIME_DEFAULTS} == meta_metrics.TIME_DEFAULTS
    explicit = VersionSpec(**(spec.model_dump() | {"time_format": "yyyymm", "data_grain": "month",
                                                  "time_aggregation": "last", "value_kind": "count",
                                                  "polarity": "down_good"}))
    for key in (*meta_metrics.TIME_DEFAULTS, "value_kind", "polarity"):
        assert key in meta_metrics.EDITABLE_FIELDS and key in MetricVersionOut.model_fields
        assert explicit.as_spec()[key] == getattr(explicit, key)
    assert {"value_kind", "polarity"}.issubset(meta_metrics.DISPLAY_FIELDS)
    assert not set(meta_metrics.TIME_DEFAULTS).intersection(meta_metrics.DISPLAY_FIELDS)


@pytest.mark.parametrize("field,value", [
    ("time_format", "unknown"), ("data_grain", "hour"), ("time_aggregation", "median"),
    ("value_kind", "unknown"), ("polarity", "unknown"),
])
def test_metric_time_invalid_enums_are_rejected(field, value):
    from pydantic import ValidationError

    from avelis.api.governance import VersionSpec

    with pytest.raises(MetaInvalid, match=field):
        meta_metrics.validate_spec(None, meta_version(**{field: value}))
    with pytest.raises(ValidationError):
        VersionSpec(description="指标甲", base_asset_id="资产甲", expression="SUM(b.value_a)",
                    time_column="b.time_a", **{field: value})


@pytest.mark.parametrize("data_type", ["date", "datetime", "timestamp with time zone", "datetimeoffset"])
def test_metric_time_default_keeps_date_type_validation(monkeypatch, data_type):
    assert meta_validate(monkeypatch, data_type=data_type)["dependency"]
    with pytest.raises(MetaInvalid, match="必须是日期或时间类型"):
        meta_validate(monkeypatch, data_type="int")


@pytest.mark.parametrize("data_type,kind", [
    ("bigint", "integer"), ("Int32", "integer"), ("UInt64", "integer"), ("int unsigned", "integer"),
    ("varchar(8)", "string"), ("nvarchar(10)", "string"), ("character varying(8)", "string"),
    ("String", "string"), ("FixedString(8)", "string"),
    ("Nullable(Int32)", "integer"), ("LowCardinality(Nullable(String))", "string"),
])
def test_metric_encoded_time_accepts_only_integer_or_string(monkeypatch, data_type, kind):
    assert meta_metrics.encoded_time_type(data_type) == kind
    assert meta_validate(monkeypatch, data_type=data_type, time_format="yyyymmdd")["dependency"]


@pytest.mark.parametrize("data_type", ["date", "decimal(8, 0)", "float", "boolean", "unknown"])
def test_metric_encoded_time_rejects_unknown_or_non_key_type(monkeypatch, data_type):
    with pytest.raises(MetaInvalid, match="必须是整数或字符串类型"):
        meta_validate(monkeypatch, data_type=data_type, time_format="yyyymmdd")


@pytest.mark.parametrize("grain", ["day", "week"])
def test_metric_month_encoding_rejects_finer_data_grain(monkeypatch, grain):
    with pytest.raises(MetaInvalid, match="要求 data_grain"):
        meta_validate(monkeypatch, data_type="integer", time_format="yyyymm", data_grain=grain)


@pytest.mark.parametrize("grain", ["month", "quarter", "year"])
def test_metric_month_encoding_accepts_coarse_data_grain(monkeypatch, grain):
    assert meta_validate(monkeypatch, data_type="integer", time_format="yyyymm", data_grain=grain)["dependency"]


def test_metric_dashed_date_encoding_requires_string(monkeypatch):
    assert meta_validate(monkeypatch, data_type="varchar(10)", time_format="yyyy-mm-dd")["dependency"]
    with pytest.raises(MetaInvalid, match="要求时间字段为字符串类型"):
        meta_validate(monkeypatch, data_type="integer", time_format="yyyy-mm-dd")


def test_metric_time_fingerprint_preserves_legacy_defaults_and_tracks_semantics():
    from avelis.connectors.base import sha256_json

    version = meta_version()
    original = {key: getattr(version, key) for key in meta_metrics.EDITABLE_FIELDS
                if key not in (*meta_metrics.DISPLAY_FIELDS, *meta_metrics.TIME_DEFAULTS)}
    legacy = sha256_json({"spec": original, "deps": []})
    assert meta_metrics.dependency_fingerprint(version, []) == legacy
    version.data_grain, version.time_aggregation = "day", "sum"
    assert meta_metrics.dependency_fingerprint(version, []) == legacy
    version.value_kind, version.polarity = "ratio", "down_good"
    assert meta_metrics.dependency_fingerprint(version, []) == legacy
    for key, value in (("time_format", "yyyymmdd"), ("data_grain", "month"), ("time_aggregation", "last")):
        setattr(version, key, value)
        assert meta_metrics.dependency_fingerprint(version, []) != legacy
        setattr(version, key, meta_metrics.TIME_DEFAULTS[key])


def test_metric_time_description_contains_version_difference_fields():
    metric = SimpleNamespace(id="指标甲", code="metric_a", name="指标甲", owner_id="用户甲", domain_id="业务域甲")
    version = meta_version(metric_id="指标甲", time_format="yyyymm", data_grain="month", time_aggregation="avg",
                           value_kind="level", polarity="up_good")
    out = meta_metrics.describe_version(SimpleNamespace(get=lambda *args: metric), version)
    for key in (*meta_metrics.TIME_DEFAULTS, "value_kind", "polarity"):
        assert out[key] == getattr(version, key)

def comp_query(*, engine="postgres", grain=None, data_grain="day", time_format=None, time_type="date",
               start="2026-01-01", end="2026-12-31", bucket=None, group_by=None, reserved=(), limit=None):
    from dataclasses import replace
    from datetime import date

    from avelis.ask.compiler import compile_metric_query
    from avelis.ask.intent import Intent
    from avelis.engines import get_engine

    dialect = replace(get_engine(engine).sql, default_catalog="catalog_a")
    version = SimpleNamespace(dimensions=[{"name": "维度甲", "column": "dimension_a"},
                                          {"name": "_time_bucket", "column": "dimension_b"}],
                              expression="SUM(measure_a)", time_column="time_a", filters=None,
                              data_grain=data_grain, time_format=time_format, timezone="America/New_York")
    asset = SimpleNamespace(catalog_name="catalog_a", db_name="schema_a", name="asset_a")
    intent = Intent(metric_code="metric_a", start_date=date.fromisoformat(start), end_date=date.fromisoformat(end),
                    group_by=group_by or [], time_grain=grain, limit=limit)
    return compile_metric_query(version=version, aliases={"b": asset}, steps=[], intent=intent, max_rows=100,
                                time_type=time_type, dialect=dialect, bucket_grain=bucket,
                                reserved_names=reserved), dialect


@pytest.mark.parametrize("engine,week_sql,quarter_sql", [
    ("postgres", "DATE_TRUNC('WEEK', b.time_a)", "DATE_TRUNC('QUARTER', b.time_a)"),
    ("mysql", "DATE_SUB(b.time_a, INTERVAL (WEEKDAY(b.time_a)) DAY)", "QUARTER(b.time_a) * 3 - 2"),
    ("starrocks", "DATE_TRUNC('WEEK', b.time_a)", "DATE_TRUNC('QUARTER', b.time_a)"),
    ("doris", "DATE_TRUNC(b.time_a, 'WEEK')", "DATE_TRUNC(b.time_a, 'QUARTER')"),
    ("sqlserver", "DATETRUNC(ISO_WEEK, b.time_a)", "DATETRUNC(QUARTER, b.time_a)"),
    ("oracle", "TRUNC(b.time_a, 'IW')", "TRUNC(b.time_a, 'Q')"),
    ("clickhouse", "dateTrunc('WEEK', b.time_a)", "dateTrunc('QUARTER', b.time_a)"),
])
@pytest.mark.parametrize("grain", ["day", "week", "month", "quarter", "year"])
def test_time_grain_sql_for_each_engine(engine, week_sql, quarter_sql, grain):
    from avelis.ask.compiler import validate_sql

    compiled, dialect = comp_query(engine=engine, grain=grain)
    assert compiled.columns == ["period", "value"]
    assert compiled.plan["time_grain"] == grain
    if grain == "week":
        assert week_sql in compiled.sql
    if grain == "quarter":
        assert quarter_sql in compiled.sql
    validate_sql(compiled.sql, dialect=dialect, allowed_tables={"catalog_a.schema_a.asset_a"},
                 schema={"catalog_a.schema_a.asset_a": {"measure_a", "time_a"}}, max_rows=100)


@pytest.mark.parametrize("time_format,time_type,low,high", [
    ("yyyymmdd", "int", "20260101", "20261231"),
    ("yyyymmdd", "varchar(8)", "'20260101'", "'20261231'"),
    ("yyyy-mm-dd", "varchar(10)", "'2026-01-01'", "'2026-12-31'"),
    ("yyyymm", "integer", "202601", "202612"),
    ("yyyymm", "String", "'202601'", "'202612'"),
])
def test_encoded_date_filter_literals(time_format, time_type, low, high):
    compiled, _ = comp_query(time_format=time_format, time_type=time_type,
                             data_grain="month" if time_format == "yyyymm" else "day")
    assert f"b.time_a BETWEEN {low} AND {high}" in compiled.sql


@pytest.mark.parametrize("start,end", [("2026-01-02", "2026-01-31"), ("2026-01-01", "2026-02-27")])
def test_month_encoded_window_must_contain_whole_months(start, end):
    with pytest.raises(MetaInvalid, match="数据按月编码，窗口必须是整月"):
        comp_query(time_format="yyyymm", time_type="int", data_grain="month", start=start, end=end)


@pytest.mark.parametrize("grain,data_grain", [("day", "week"), ("week", "month"), ("month", "quarter"),
                                               ("quarter", "year")])
def test_time_grain_cannot_be_finer_than_data(grain, data_grain):
    with pytest.raises(MetaInvalid, match="不能比数据粒度"):
        comp_query(grain=grain, data_grain=data_grain)


@pytest.mark.parametrize("grain", ["week", "quarter", "year"])
def test_encoded_date_unsupported_grain_is_explicit(grain):
    with pytest.raises(MetaInvalid, match="编码日期键只支持 day 与 month"):
        comp_query(grain=grain, time_format="yyyymmdd", time_type="int")


@pytest.mark.parametrize("time_format,time_type,grain,expected", [
    ("yyyymmdd", "int", "month", "DIV(b.time_a, 100)"),
    ("yyyymmdd", "varchar(8)", "month", "SUBSTRING(b.time_a FROM 5 FOR 2)"),
    ("yyyymmdd", "varchar(8)", "day", "SUBSTRING(b.time_a FROM 7 FOR 2)"),
    ("yyyy-mm-dd", "varchar(10)", "month", "SUBSTRING(b.time_a FROM 1 FOR 7)"),
    ("yyyy-mm-dd", "varchar(10)", "day", "b.time_a AS period"),
    ("yyyymm", "int", "month", "CAST(b.time_a AS VARCHAR)"),
])
def test_encoded_date_period_uses_declared_encoding(time_format, time_type, grain, expected):
    compiled, _ = comp_query(grain=grain, time_format=time_format, time_type=time_type)
    assert expected in compiled.sql
    assert "TO_CHAR" not in compiled.sql


def test_time_bucket_keeps_output_period_and_avoids_collisions():
    compiled, _ = comp_query(grain="month", bucket="week", data_grain="week", group_by=["_time_bucket"],
                             reserved=("_time_bucket_1",), limit=2)
    assert compiled.columns == ["period", "_time_bucket", "_time_bucket_2", "value"]
    assert compiled.plan["bucket_column"] == "_time_bucket_2"
    assert compiled.plan["bucket_grain"] == "week"
    assert "TO_CHAR(b.time_a, 'YYYY-MM')" in compiled.sql
    assert "TO_CHAR(DATE_TRUNC('WEEK', b.time_a), 'YYYY-MM-DD')" in compiled.sql
    assert compiled.sql.endswith("LIMIT 101")


def test_sqlserver_datetimeoffset_boundaries_follow_dst():
    compiled, _ = comp_query(engine="sqlserver", time_type="datetimeoffset(7)",
                             start="2026-03-08", end="2026-03-08")
    assert "2026-03-08 00:00:00-05:00" in compiled.sql
    assert "2026-03-09 00:00:00-04:00" in compiled.sql
    with pytest.raises(MetaInvalid, match="datetimeoffset 时间列不支持按时间分组"):
        comp_query(engine="sqlserver", time_type="datetimeoffset", grain="day")
    with pytest.raises(MetaInvalid, match="datetimeoffset 时间列不支持按时间分组"):
        comp_query(engine="sqlserver", time_type="datetimeoffset", bucket="day")


@pytest.mark.parametrize("start,end,expected", [
    ("2026-03-01", "2026-03-31", ("2026-02-01", "2026-02-28")),
    ("2028-03-01", "2028-03-31", ("2028-02-01", "2028-02-29")),
    ("2026-01-01", "2026-03-31", ("2025-10-01", "2025-12-31")),
    ("2026-01-01", "2026-12-31", ("2025-01-01", "2025-12-31")),
])
def test_previous_calendar_uses_matching_natural_period(start, end, expected):
    from datetime import date

    from avelis.ask.intent import comparison_range

    actual = comparison_range(date.fromisoformat(start), date.fromisoformat(end), "previous_calendar")
    assert tuple(str(d) for d in actual) == expected


@pytest.mark.parametrize("start,end", [("2026-01-02", "2026-01-31"), ("2026-01-01", "2026-02-28"),
                                     ("2026-02-01", "2026-04-30")])
def test_previous_calendar_rejects_non_natural_periods(start, end):
    from datetime import date

    from avelis.ask.intent import comparison_range

    with pytest.raises(MetaInvalid, match="请用 previous_period 或 custom"):
        comparison_range(date.fromisoformat(start), date.fromisoformat(end), "previous_calendar")


def test_submit_intent_threads_time_grain_and_calendar_compare():
    import json
    from datetime import date

    from avelis.ask.agent import SubmitArgs, Toolbox
    from avelis.ask.intent import MetricCard

    card = MetricCard(code="metric_a", name="指标甲", aliases=[], dimensions=[])
    toolbox = Toolbox(None, None, SimpleNamespace(cards=[card], versions={}))
    for grain in ("day", "week", "month", "quarter", "year"):
        args = SubmitArgs(metric_codes=["metric_a"], start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
                          time_grain=grain)
        assert json.loads(toolbox.submit_intent(**args.model_dump()))["accepted"]
        assert toolbox.intent.time_grain == grain
    args = SubmitArgs(metric_codes=["metric_a"], start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
                      compare="previous_calendar")
    assert json.loads(toolbox.submit_intent(**args.model_dump()))["accepted"]
    assert toolbox.intent.compare == "previous_calendar"
    assert "季初" in SubmitArgs.model_json_schema()["properties"]["time_grain"]["description"]

# CONNECTOR_TESTS


class conn_Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = ([conn_SimpleNamespace(name="value")] if connection.postgres else [("value",)])

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement):
        statement = statement.as_string() if hasattr(statement, "as_string") else statement
        self.connection.statements.append(statement)
        if statement.startswith("SET time_zone") and self.connection.reject_timezone:
            self.connection.reject_timezone -= 1
            raise conn_pymysql.err.OperationalError(1298, "Unknown or incorrect time zone")
        if statement.startswith("SET LOCAL TIME ZONE"):
            assert self.connection.in_transaction
            self.connection.timezone = statement.split("'")[1]
        if statement == "SELECT 1":
            self.connection.query_timezones.append(self.connection.timezone)
        return self

    def fetchmany(self, count):
        return [(1,)]

    def fetchone(self):
        return (1,)


class conn_Connection:
    def __init__(self, *, postgres=False, reject_timezone=0):
        self.postgres, self.reject_timezone = postgres, reject_timezone
        self.statements, self.query_timezones = [], []
        self.closed, self.in_transaction, self.timezone = False, False, "UTC"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False

    @conn_contextmanager
    def transaction(self):
        original_timezone = self.timezone
        self.in_transaction = True
        try:
            yield
        finally:
            self.timezone, self.in_transaction = original_timezone, False

    def execute(self, statement):
        return self.cursor().execute(statement)

    def cursor(self, *args, **kwargs):
        return conn_Cursor(self)

    def autocommit(self, value):
        pass

    def rollback(self):
        self.statements.append("ROLLBACK")

    def close(self):
        self.closed = True


def conn_make_connector(kind):
    classes = {"postgres": conn_PostgresConnector, "mysql": conn_MySQLConnector,
               "starrocks": conn_StarRocksConnector, "doris": conn_DorisConnector,
               "oracle": conn_OracleConnector, "clickhouse": conn_ClickHouseConnector,
               "sqlserver": conn_SqlServerConnector}
    return classes[kind]("unit-host", 1, "unit-user", "unit-password", {"database": "unitdb", "service": "UNIT"})


@conn_pytest.mark.parametrize("kind", ["postgres", "mysql", "starrocks", "doris", "sqlserver", "oracle", "clickhouse"])
def test_connector_business_timezone_sessions(monkeypatch, kind):
    connector = conn_make_connector(kind)
    connection, settings_seen = conn_Connection(postgres=kind == "postgres"), []
    if kind == "clickhouse":
        def conn_query(sql, *, settings):
            connection.statements.append(sql)
            settings_seen.append(settings)
            return conn_SimpleNamespace(result_rows=[(1,)], column_names=["value"])
        monkeypatch.setattr(connector, "_client", lambda **kw: conn_SimpleNamespace(
            query=conn_query, close=connection.close))
    else:
        monkeypatch.setattr(connector, "_connect", lambda **kw: connection)
    result = connector.execute_select("SELECT 1", timeout_seconds=3, mem_limit_bytes=1024, max_rows=10,
                                      timezone="Asia/Kathmandu")
    assert result.rows == [[1]] and result.warnings == [] and connection.closed
    if kind == "postgres":
        assert "SET LOCAL TIME ZONE 'Asia/Kathmandu'" in connection.statements
        assert connection.query_timezones == ["Asia/Kathmandu"] and connection.timezone == "UTC"
    elif kind in ("mysql", "starrocks", "doris"):
        assert connection.statements[0] == "SET time_zone = 'Asia/Kathmandu'"
    elif kind == "oracle":
        assert connection.statements[:2] == ["ALTER SESSION SET TIME_ZONE = 'Asia/Kathmandu'",
                                             "SET TRANSACTION READ ONLY"]
    elif kind == "clickhouse":
        assert settings_seen == [{"max_execution_time": 3, "max_memory_usage": 1024,
                                  "session_timezone": "Asia/Kathmandu"}]
    else:
        assert connection.statements == ["SELECT 1", "ROLLBACK"]


@conn_pytest.mark.parametrize("kind", ["mysql", "starrocks", "doris"])
def test_connector_timezone_fixed_offset_retry_is_reported(monkeypatch, kind):
    connector, connection = conn_make_connector(kind), conn_Connection(reject_timezone=1)
    monkeypatch.setattr(connector, "_connect", lambda **kw: connection)
    monkeypatch.setattr("avelis.connectors.starrocks.current_timezone_offset", lambda name: "+05:45")
    result = connector.execute_select("SELECT 1", timeout_seconds=3, mem_limit_bytes=1024, max_rows=10,
                                      timezone="Asia/Kathmandu")
    assert connection.statements[:2] == ["SET time_zone = 'Asia/Kathmandu'", "SET time_zone = '+05:45'"]
    assert len(result.warnings) == 1 and "查询时刻的固定偏移 +05:45" in result.warnings[0]


@conn_pytest.mark.parametrize("kind", ["mysql", "starrocks", "doris"])
def test_connector_timezone_retry_only_once(monkeypatch, kind):
    connector, connection = conn_make_connector(kind), conn_Connection(reject_timezone=2)
    monkeypatch.setattr(connector, "_connect", lambda **kw: connection)
    monkeypatch.setattr("avelis.connectors.starrocks.current_timezone_offset", lambda name: "+05:45")
    with conn_pytest.raises(conn_QueryError):
        connector.execute_select("SELECT 1", timeout_seconds=3, mem_limit_bytes=1024, max_rows=10,
                                 timezone="Asia/Kathmandu")
    assert connection.statements == ["SET time_zone = 'Asia/Kathmandu'", "SET time_zone = '+05:45'"]
    assert connection.closed


def test_connector_postgres_local_timezone_does_not_leak(monkeypatch):
    connector, connection = conn_make_connector("postgres"), conn_Connection(postgres=True)
    monkeypatch.setattr(connector, "_connect", lambda **kw: connection)
    connector.execute_select("SELECT 1", timeout_seconds=3, mem_limit_bytes=0, max_rows=10,
                             timezone="Asia/Kathmandu")
    connector.execute_select("SELECT 1", timeout_seconds=3, mem_limit_bytes=0, max_rows=10)
    assert connection.query_timezones == ["Asia/Kathmandu", "UTC"]


@conn_pytest.mark.parametrize("kind", ["postgres", "starrocks"])
def test_connector_exploration_uses_business_timezone(monkeypatch, kind):
    connector, connection = conn_make_connector(kind), conn_Connection(postgres=kind == "postgres")
    monkeypatch.setattr(connector, "_connect", lambda **kw: connection)
    connector.execute_explore("SELECT 1", timeout_seconds=3, mem_limit_bytes=1024, max_rows=10,
                              timezone="Asia/Kathmandu")
    expected = ("SET LOCAL TIME ZONE 'Asia/Kathmandu'" if kind == "postgres"
                else "SET time_zone = 'Asia/Kathmandu'")
    assert expected in connection.statements


def test_connector_fixed_offset_keeps_fractional_hours():
    assert conn_current_offset("Asia/Kathmandu") == "+05:45"
    assert conn_current_offset("Etc/UTC") == "+00:00"


def test_connector_unknown_timezone_rejected_before_query(monkeypatch):
    connector, connection = conn_make_connector("starrocks"), conn_Connection()
    monkeypatch.setattr(connector, "_connect", lambda **kw: connection)
    with conn_pytest.raises(conn_QueryError, match="业务时区无效"):
        connector.execute_select("SELECT 1", timeout_seconds=3, mem_limit_bytes=1024, max_rows=10,
                                 timezone="Unknown/Zone")
    assert connection.statements == [] and connection.closed


def test_connector_fake_accepts_timezone_and_preserves_row_limit():
    connector = conn_FakeConnector({"query_result": {"columns": ["指标甲"], "rows": [[1], [2]]}})
    result = connector.execute_select("SELECT 1", timeout_seconds=3, mem_limit_bytes=0, max_rows=1,
                                      timezone="Asia/Kathmandu")
    assert result.rows == [[1]] and result.truncated

# ANALYSIS_TESTS

@pytest.mark.parametrize("grain,start,end,labels", [
    ("day", "2026-01-30", "2026-02-01", ["2026-01-30", "2026-01-31", "2026-02-01"]),
    ("week", "2026-01-01", "2026-01-11", ["2025-12-29", "2026-01-05"]),
    ("month", "2026-01-20", "2026-03-01", ["2026-01", "2026-02", "2026-03"]),
    ("quarter", "2026-02-01", "2026-07-31", ["2026-01-01", "2026-04-01", "2026-07-01"]),
    ("year", "2025-12-01", "2027-01-31", ["2025", "2026", "2027"]),
])
def test_analysis_coverage_uses_declared_period_labels(grain, start, end, labels):
    from datetime import date

    from avelis.analysis import calc

    rows = [{"维度甲": "成员甲", "period": value} for value in labels[::2]]
    out = calc.coverage(rows, ["维度甲"], "period", date.fromisoformat(start), date.fromisoformat(end), grain=grain)
    assert out["summary"]["expected_periods"] == len(labels)
    assert out["rows"][0]["missing_periods"] == ",".join(labels[1::2])
    assert out["rows"][0]["missing_count"] == len(labels[1::2])


def test_analysis_empty_governed_coverage_reports_all_missing_periods():
    from datetime import date

    from avelis.analysis import calc

    out = calc.coverage([], [], "period", date(2026, 1, 1), date(2026, 3, 31), grain="month")
    assert out["summary"]["expected_periods"] == 3
    assert out["rows"][0]["missing_count"] == 3
    assert out["rows"][0]["missing_periods"] == "2026-01,2026-02,2026-03"


def test_analysis_coverage_does_not_guess_undeclared_monthly_dates():
    from datetime import date

    from avelis.analysis import calc

    out = calc.coverage([{"period": "2026-01"}], [], "period", date(2026, 1, 1), date(2026, 3, 31))
    assert out["summary"]["expected_periods"] is None
    assert out["rows"][0]["missing_count"] is None
    with pytest.raises(calc.CalcError, match="期间列"):
        calc.coverage([{"period": "202601"}], [], "period", date(2026, 1, 1), date(2026, 3, 31), grain="month")


@pytest.mark.parametrize("grain", ["day", "week", "month", "quarter", "year"])
def test_analysis_query_and_scope_schemas_accept_five_grains(grain):
    from avelis.analysis.tools import QueryArgs, Scope

    assert QueryArgs(metric_codes=["metric_a"], start_date="2026-01-01", end_date="2026-01-31",
                     time_grain=grain, purpose="指标甲趋势").time_grain == grain
    assert Scope(question_type="lookup", metric_codes=["metric_a"], time_grain=grain).time_grain == grain
    args = QueryArgs(metric_codes=["metric_a"], start_date="2026-01-01", end_date="2026-01-31",
                     compare="previous_calendar", purpose="指标甲对比")
    assert args.compare == "previous_calendar"


@pytest.mark.parametrize("grain", ["day", "week", "month", "quarter", "year"])
def test_analysis_precheck_queries_at_data_grain(monkeypatch, grain):
    from avelis.analysis import tools as ana_tools

    box = object.__new__(ana_tools.AnalysisToolbox)
    box.run = SimpleNamespace(window={"start": "2026-04-01", "end": "2026-06-30",
                                      "compare_start": "2026-01-01", "compare_end": "2026-03-31"})
    box._datasets = {}
    box.describe_metric = lambda **kw: json.dumps({"data_grain": grain})
    box.changes = box.data_health = box.explain_change = lambda **kw: "{}"
    box._source_metadata = lambda code: []
    box._metric = lambda code: (SimpleNamespace(name="指标甲"),
                                SimpleNamespace(kind="base", data_grain=grain))
    calls = []
    box.query_metric = lambda **kw: calls.append(kw) or "{}"
    monkeypatch.setattr(ana_tools, "get_settings", lambda: SimpleNamespace(
        analysis_investigation=False, analysis_retrieval=False))
    box.precheck(ana_tools.Scope(question_type="change", metric_codes=["metric_a"]))
    assert len(calls) == 1 and calls[0]["time_grain"] == grain


@pytest.mark.parametrize("grain", ["week", "month", "quarter", "year"])
def test_analysis_time_concentration_explains_non_daily_unknown(grain):
    from avelis.analysis import investigation

    dataset = SimpleNamespace(kind="governed", truncated=False, meta={"intent": {
        "time_grain": grain, "metric_codes": ["metric_a"]}})
    out = investigation._time_distribution(dataset, {})
    assert out["concentrated"] is None and out["max_day_share"] is None
    assert "只适用于日粒度" in out["reason"]


def ana_calendar_box(monkeypatch, aggregation, *, nested=False):
    from avelis.ask.service import Catalog
    from avelis.models import MetricVersion
    from tests.unit.test_analysis_toolbox import make_box as ana_make_box

    metric = SimpleNamespace(id="指标甲", code="metric_a", name="指标甲", domain_id="dA")
    leaf = SimpleNamespace(id="leaf_a", kind="base", expression="SUM(value_a)", time_aggregation=aggregation,
                           data_grain="month", unit=None, unit_scale=1)
    root = leaf
    versions = [leaf]
    if nested:
        middle = SimpleNamespace(id="middle_a", kind="derived", expression="metric_c", time_aggregation="sum",
                                 data_grain="day", components=[{"version_id": leaf.id}])
        root = SimpleNamespace(id="root_a", kind="derived", expression="metric_b", time_aggregation="sum",
                               data_grain="day", components=[{"version_id": middle.id}])
        versions += [middle, root]
    box = ana_make_box(monkeypatch=monkeypatch, catalog=Catalog([], {"metric_a": (metric, root)}))
    for version in versions:
        box.session.store[(MetricVersion, version.id)] = version
    ds = box._dataset("governed", "指标甲对比", ["指标甲", "指标甲·对比期"], [[12, 10]], meta={
        "versions": {"metric_a": root.id}, "intent": {"metric_codes": ["metric_a"], "compare": "custom",
        "start_date": "2026-02-01", "end_date": "2026-02-28", "compare_start": "2026-01-01",
        "compare_end": "2026-01-31"}})
    return box, f"D{ds.seq}", root


@pytest.mark.parametrize("aggregation", ["last", "avg"])
@pytest.mark.parametrize("nested", [False, True])
def test_analysis_calendar_rejects_nonadditive_time_and_nested_components(monkeypatch, aggregation, nested):
    import json

    box, ref, _ = ana_calendar_box(monkeypatch, aggregation, nested=nested)
    out = json.loads(box.calendar_adjust(dataset=ref, current="指标甲", previous="指标甲·对比期"))
    assert "该指标跨时间不能累加，不能按天数折算" in out["error"]


def test_analysis_calendar_default_sum_keeps_existing_result(monkeypatch):
    import json

    box, ref, _ = ana_calendar_box(monkeypatch, "sum")
    out = json.loads(box.calendar_adjust(dataset=ref, current="指标甲", previous="指标甲·对比期"))
    assert "error" not in out
    assert out["summary"]["change"] == "2"


def test_analysis_derived_grain_resolves_locked_components(monkeypatch):
    box, _, version = ana_calendar_box(monkeypatch, "sum", nested=True)
    assert box._data_grain(version) == "month"


def test_analysis_governed_monthly_coverage_uses_query_grain(monkeypatch):
    import json
    from datetime import date

    from tests.unit.test_analysis_toolbox import make_box as ana_make_box

    box = ana_make_box(monkeypatch=monkeypatch)
    box.session.store[(MetaMetricVersion, "version_a")] = SimpleNamespace(id="version_a", kind="base", data_grain="month")
    ds = box._dataset("governed", "指标甲趋势", ["period", "指标甲"], [["2026-01", 1], ["2026-03", 3]], meta={
        "versions": {"metric_a": "version_a"},
        "time_column": "period", "data_grains": {"metric_a": "month"},
        "intent": {"time_grain": "month", "metric_codes": ["metric_a"]}})
    out = json.loads(box.coverage(dataset=f"D{ds.seq}", start_date=date(2026, 1, 1), end_date=date(2026, 3, 31)))
    assert "error" not in out and out["summary"]["missing_total"] == 1
    assert box._datasets[out["dataset"]].rows[0][-1] == "2026-02"


@pytest.mark.parametrize("engine", ["postgres", "starrocks"])
def test_analysis_exploration_passes_business_timezone(monkeypatch, engine):
    import json

    from avelis.analysis import tools as ana_tools
    from avelis.config import build_settings
    from tests.unit.test_analysis_explore_tool import explore_box as ana_explore_box

    box, connector = ana_explore_box(monkeypatch, engine)
    monkeypatch.setattr(ana_tools, "get_settings", lambda: build_settings({
        "AVELIS_SECRET": "offline-unit-test-secret-0123456789", "AVELIS_BUSINESS_TIMEZONE": "America/New_York"}))
    out = json.loads(box.query_sql(sql="SELECT region, SUM(amount) AS total FROM d1.facts GROUP BY region",
                                   purpose="指标甲的补充证据"))
    assert "error" not in out
    executed = [call for call in connector.calls if call[0] == "execute"]
    assert executed[0][2]["timezone"] == "America/New_York"


def ana_coverage_box(monkeypatch):
    box, _, version = ana_calendar_box(monkeypatch, "sum")
    ds = box._dataset("governed", "指标甲趋势", ["period", "指标甲"], [["2026-01", 1], ["2026-03", 3]], meta={
        "time_column": "period", "versions": {"metric_a": version.id},
        "data_grains": {"metric_a": "month"}, "intent": {"time_grain": "month", "metric_codes": ["metric_a"]}})
    return box, ds, version


@pytest.mark.parametrize("problem,reason", [
    ("missing_versions", "缺少锁定的查询版本"),
    ("missing_one_version", "缺少锁定的查询版本"),
    ("unknown_grain", "数据粒度未知"),
    ("finer_intent", "查询粒度比数据粒度更细"),
    ("inconsistent_grains", "与锁定版本不一致"),
    ("incomplete_grains", "数据粒度不完整"),
])
def test_analysis_coverage_rejects_incomplete_or_conflicting_metadata(monkeypatch, problem, reason):
    import json
    from datetime import date

    box, ds, version = ana_coverage_box(monkeypatch)
    if problem == "missing_versions":
        ds.meta.pop("versions")
    elif problem == "missing_one_version":
        ds.meta["intent"]["metric_codes"].append("metric_b")
    elif problem == "unknown_grain":
        version.data_grain = "hour"
    elif problem == "finer_intent":
        ds.meta["intent"]["time_grain"] = "day"
    elif problem == "inconsistent_grains":
        ds.meta["data_grains"]["metric_a"] = "day"
    elif problem == "incomplete_grains":
        ds.meta["intent"]["metric_codes"].append("metric_b")
        ds.meta["versions"]["metric_b"] = version.id
    out = json.loads(box.coverage(dataset=f"D{ds.seq}", start_date=date(2026, 1, 1), end_date=date(2026, 3, 31)))
    assert reason in out["error"]


def test_analysis_coverage_reads_locked_versions_when_legacy_grains_are_absent(monkeypatch):
    import json
    from datetime import date

    box, ds, _ = ana_coverage_box(monkeypatch)
    ds.meta.pop("data_grains")
    out = json.loads(box.coverage(dataset=f"D{ds.seq}", start_date=date(2026, 1, 1), end_date=date(2026, 3, 31)))
    assert "error" not in out and out["summary"]["missing_total"] == 1


@pytest.mark.parametrize("engine", ["postgres", "starrocks"])
def test_analysis_exploration_preserves_timezone_fallback_warnings(monkeypatch, engine):
    import json

    from avelis.connectors.base import QueryResult
    from tests.unit.test_analysis_explore_tool import (
        FakeConnector as AnaConnector,
    )
    from tests.unit.test_analysis_explore_tool import (
        audits as ana_audits,
    )
    from tests.unit.test_analysis_explore_tool import (
        explore_box as ana_explore_box,
    )

    warnings = ["业务时区名称不可用，当前查询改用固定偏移 +08:00"]
    connector = AnaConnector(result=QueryResult(columns=["region", "total"], rows=[["成员甲", 1]],
                                               truncated=False, warnings=warnings))
    box, _ = ana_explore_box(monkeypatch, engine, connector=connector)
    out = json.loads(box.query_sql(sql="SELECT region, SUM(amount) AS total FROM d1.facts GROUP BY region",
                                   purpose="指标甲的补充证据"))
    assert out["warnings"] == warnings
    assert box._datasets[out["dataset"]].meta["query_warnings"] == warnings
    assert ana_audits(box)[-1]["query_warnings"] == warnings
    assert json.loads(box.view_dataset(ref=out["dataset"]))["warnings"] == warnings

# 服务合成与执行链路


def svc_state(monkeypatch, method="last", *, rows=None, max_rows=1000, output_grain=None):
    metric = svc_ns(id="指标甲", code="metric_a", name="指标甲", domain_id="域甲")
    version = svc_ns(id="版本甲", version_no=1, kind="base", status="published", time_aggregation=method,
                     data_grain="day", time_format=None, timezone="Asia/Tokyo", dependency_fingerprint="依赖甲",
                     dimensions=[{"name": "维度甲", "column": "b.member_a"}], filters=None,
                     expression="SUM(b.value_a)", time_column="b.day_a", grain=[], joins=[])
    asset = svc_ns(id="资产甲", datasource_id="来源甲", current_version_id="资产版本甲", catalog_name="catalog_a",
                   db_name="schema_a", name="table_a", kind="table")
    datasource = svc_ns(id="来源甲")
    store = {(SvcDatasource, datasource.id): datasource, (SvcMetric, metric.id): metric,
             (SvcVersion, version.id): version}
    session = svc_ns(get=lambda cls, key: store.get((cls, key)), refresh=lambda *args: None)
    settings = svc_ns(query_timeout_seconds=10, query_mem_limit_bytes=1024, query_max_rows=max_rows,
                      answer_cache_seconds=1, business_timezone="UTC")
    user = svc_ns(id="用户甲")
    calls, cached = [], {}
    monkeypatch.setattr(svc_service, "get_settings", lambda: settings)
    monkeypatch.setattr(svc_service, "describe_version", lambda *args: vars(version))
    monkeypatch.setattr(svc_service, "_version_evidence", lambda *args: {"id": version.id})
    monkeypatch.setattr(svc_service, "dependency_drift", lambda *args: [])
    monkeypatch.setattr(svc_service, "validate_spec", lambda *args: {"aliases": {"b": asset}, "steps": [],
                                                                   "dependency": []})
    monkeypatch.setattr(svc_service, "upstream_closure", lambda *args: {asset.id: asset})
    monkeypatch.setattr(svc_service.access, "domain_ids", lambda *args: {"域甲"})
    monkeypatch.setattr(svc_service.access, "can_read_asset", lambda *args: True)
    monkeypatch.setattr(svc_service, "_column_type", lambda *args: "date")
    monkeypatch.setattr(svc_service, "_columns", lambda *args: ["day_a", "member_a", "value_a"])
    monkeypatch.setattr(svc_service, "dialect_of", lambda *args: SvcDialect("postgres", "catalog_a", catalog_in_sql=False))
    monkeypatch.setattr(svc_service, "_live_check", lambda *args: [])
    monkeypatch.setattr(svc_service, "freshness_boundary", lambda *args: {})
    monkeypatch.setattr(svc_service, "_asset_evidence", lambda *args: {"qualified": "catalog_a.schema_a.table_a"})
    monkeypatch.setattr(svc_service, "_lineage_evidence", lambda *args: [])
    monkeypatch.setattr(svc_service, "_dimension_docs", lambda *args: {})
    monkeypatch.setattr(svc_service, "_cache_get", lambda key: cached.get(key))
    monkeypatch.setattr(svc_service, "_cache_put", lambda key, value, *args: cached.__setitem__(key, value))

    def execute(sql, **kwargs):
        calls.append((sql, kwargs))
        columns = [column.alias_or_name for column in svc_sqlglot.parse_one(sql, read="postgres").selects]
        result_rows = rows(sql) if callable(rows) else rows
        return SvcResult(columns=columns, rows=result_rows or [], truncated=False,
                         warnings=["会话时区回退说明"])

    monkeypatch.setattr(svc_service, "query_connector", lambda *args: svc_ns(
        execute_select=execute, freshness=lambda *args: {}))
    catalog = svc_service.Catalog([], {"metric_a": (metric, version)})

    def run(**overrides):
        intent = SvcIntent(metric_code="metric_a", start_date=svc_date(2026, 1, 1),
                           end_date=svc_date(2026, 2, 28), group_by=["维度甲"], time_grain=output_grain, **overrides)
        answer = svc_service._scratch(SvcAnswer(question="查询指标甲", model_adapter="offline"), user)
        svc_service.fulfil(session, user, intent, answer, catalog)
        return answer

    return svc_ns(run=run, calls=calls, store=store, session=session, user=user, catalog=catalog,
                  version=version, metric=metric, settings=settings)


def test_batch2_service_last_uses_global_bucket_and_passes_timezone(monkeypatch):
    state = svc_state(monkeypatch, rows=[["成员甲", "2026-01-01", 9], ["成员乙", "2026-01-02", 7]])
    answer = state.run(order="desc", limit=1)
    assert answer.status == "answered" and answer.result["rows"] == [["成员乙", "7"]]
    assert state.calls[0][1]["timezone"] == "Asia/Tokyo"
    assert "LIMIT 1001" in state.calls[0][0]
    assert answer.evidence["time_aggregation"]["method"] == "last"
    assert answer.evidence["query_warnings"] == ["会话时区回退说明"]
    repeated = state.run(order="desc", limit=1)
    assert repeated.cache_hit and len(state.calls) == 1
    assert repeated.evidence["query_warnings"] == ["会话时区回退说明"]


def test_batch2_service_last_missing_member_in_global_last_bucket_is_unknown(monkeypatch):
    state = svc_state(monkeypatch, rows=[["成员甲", "2026-01-01", 9], ["成员乙", "2026-01-02", 7]])
    answer = state.run()
    assert answer.result["columns"] == ["维度甲", "value"]
    assert answer.result["rows"] == [["成员甲", None], ["成员乙", "7"]]


def test_batch2_service_avg_uses_only_members_observed_buckets(monkeypatch):
    state = svc_state(monkeypatch, "avg", rows=[["成员甲", "2026-01-01", 2], ["成员甲", "2026-01-02", 6],
                                               ["成员乙", "2026-01-02", 10]])
    answer = state.run()
    assert answer.result["rows"] == [["成员甲", "4"], ["成员乙", "10"]]


def test_batch2_service_last_composes_each_output_period_independently(monkeypatch):
    state = svc_state(monkeypatch, output_grain="month", rows=[
        ["2026-01", "成员甲", "2026-01-01", 2], ["2026-01", "成员乙", "2026-01-31", 6],
        ["2026-02", "成员甲", "2026-02-02", 10]])
    answer = state.run()
    assert answer.result["rows"] == [["2026-01", "成员甲", None], ["2026-01", "成员乙", "6"],
                                     ["2026-02", "成员甲", "10"]]


def test_batch2_service_marks_raw_bucket_limit_as_truncated(monkeypatch):
    state = svc_state(monkeypatch, rows=[["成员甲", "2026-01-01", 2], ["成员甲", "2026-01-02", 6]], max_rows=2)
    answer = state.run()
    assert answer.result["row_count"] == 1 and answer.result["truncated"]


def test_batch2_service_sum_keeps_single_sql_aggregation(monkeypatch):
    state = svc_state(monkeypatch, "sum", rows=[["成员甲", 8]])
    answer = state.run()
    assert answer.result["rows"] == [["成员甲", 8]]
    assert "_time_bucket" not in state.calls[0][0]


def test_batch2_service_comparison_reuses_time_composition(monkeypatch):
    state = svc_state(monkeypatch, "avg", rows=[["成员甲", "2026-01-01", 2], ["成员甲", "2026-01-02", 6]])
    answer = state.run(compare="previous_period")
    assert answer.status == "answered"
    assert answer.result["rows"] == [["成员甲", "4", "4", "0", "0.0000"]]
    assert len(state.calls) == 2
    assert all(step["time_aggregation"]["method"] == "avg" for step in answer.evidence["plan"])


@svc_pytest.mark.parametrize("method", ["last", "avg"])
def test_batch2_time_composition_empty_scalar_is_unknown(method):
    columns, rows = svc_service._aggregate_time_rows(["桶甲", "value"], [], bucket_column="桶甲",
                                                     method=method, time_grain=None)
    assert columns == ["value"] and rows == [[None]]


def test_batch2_service_derived_components_keep_their_own_time_rules(monkeypatch):
    def source_rows(sql):
        values = [10, 30] if "SUM(b.value_b)" in sql else [2, 6]
        return [["成员甲", f"2026-01-0{i + 1}", value] for i, value in enumerate(values)]

    state = svc_state(monkeypatch, "last", rows=source_rows)
    other_metric = svc_ns(id="指标乙", code="metric_b", name="指标乙", domain_id="域甲")
    other_version = svc_ns(**(vars(state.version) | {"id": "版本乙", "time_aggregation": "avg",
                                                    "expression": "SUM(b.value_b)"}))
    metric = svc_ns(id="指标丙", code="metric_c", name="指标丙", domain_id="域甲")
    version = svc_ns(id="版本丙", version_no=1, kind="derived", status="published", dimensions=state.version.dimensions,
                     expression="metric_a + metric_b", components=[
                         {"metric_id": state.metric.id, "version_id": state.version.id,
                          "metric_code": "metric_a", "metric_name": "指标甲"},
                         {"metric_id": other_metric.id, "version_id": other_version.id,
                          "metric_code": "metric_b", "metric_name": "指标乙"}])
    state.store.update({(SvcMetric, other_metric.id): other_metric, (SvcVersion, other_version.id): other_version})
    state.catalog.versions.update({"metric_b": (other_metric, other_version), "metric_c": (metric, version)})
    monkeypatch.setattr(svc_service, "_columns", lambda *args: ["day_a", "member_a", "value_a", "value_b"])
    intent = SvcIntent(metric_code="metric_c", start_date=svc_date(2026, 1, 1), end_date=svc_date(2026, 1, 31),
                       group_by=["维度甲"])
    answer = svc_service._scratch(SvcAnswer(question="查询指标丙", model_adapter="offline"), state.user)
    svc_service.fulfil(state.session, state.user, intent, answer, state.catalog)
    assert answer.status == "answered" and answer.result["rows"] == [["成员甲", "26.0000"]]
    assert [step["time_aggregation"]["method"] for step in answer.evidence["plan"]] == ["last", "avg"]


def test_batch2_service_parses_quoted_encoded_time_column_before_type_lookup(monkeypatch):
    state = svc_state(monkeypatch, "sum", rows=[["成员甲", 8]])
    state.version.time_column = 'b."day_a"'
    state.version.time_format = "yyyymmdd"
    looked_up = []

    def column_type(session, asset, column):
        looked_up.append(column)
        return "integer" if column == "day_a" else ""

    monkeypatch.setattr(svc_service, "_column_type", column_type)
    answer = state.run()
    assert answer.status == "answered" and looked_up == ["day_a"]
    assert "BETWEEN 20260101 AND 20260228" in state.calls[0][0]


def test_batch2_service_invalid_previous_calendar_is_rejected_without_query(monkeypatch):
    state = svc_state(monkeypatch, "sum")
    answer = state.run(compare="previous_calendar")
    assert answer.status == "error" and "请用 previous_period 或 custom" in answer.message
    assert state.calls == []


def test_batch2_service_derived_declared_data_grain_rejects_finer_output(monkeypatch):
    state = svc_state(monkeypatch)
    state.version.kind = "derived"
    state.version.data_grain = "month"
    intent = SvcIntent(metric_code="metric_a", start_date=svc_date(2026, 1, 1), end_date=svc_date(2026, 1, 31),
                       time_grain="day")
    answer = svc_service._scratch(SvcAnswer(question="查询指标甲", model_adapter="offline"), state.user)
    svc_service.fulfil(state.session, state.user, intent, answer, state.catalog)
    assert answer.status == "error" and "不能比" in answer.message
    assert state.calls == []


@svc_pytest.mark.parametrize("exact", [False, True])
def test_batch2_dimension_sample_and_exact_check_use_metric_timezone(monkeypatch, exact):
    from tests.unit.test_overfit_batch1 import _batch1_dimension_reader

    session, version, state, _ = _batch1_dimension_reader(monkeypatch, SvcDialect("postgres", "catalog_a"))
    version.timezone = "Asia/Tokyo"
    state["rows"] = [[1]] if exact else [["成员甲"]]
    monkeypatch.setattr(svc_service.cache, "client", lambda: svc_ns(get=lambda *args: None, set=lambda *args, **kw: None))
    settings = svc_ns(business_timezone="UTC")
    if exact:
        assert svc_service._existing_dimension_values(session, object(), version, "维度甲", ["成员甲"], settings) == {"成员甲"}
    else:
        assert svc_service._dimension_values(session, object(), version, settings) == {"维度甲": ["成员甲"]}
    assert state["queries"][0][1]["timezone"] == "Asia/Tokyo"
