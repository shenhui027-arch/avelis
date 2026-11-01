"""业务日期按时间列类型显式转时区，不能依赖会话设置或列自带的时区。"""

from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest
import sqlglot
from sqlglot import exp

from avelis.ask.compiler import compile_metric_query, validate_sql
from avelis.ask.intent import Intent
from avelis.engines import get_engine
from avelis.governance.errors import Invalid


def compile_time(engine, data_type, *, grain=None, bucket=None, timezone="America/New_York",
                 start="2026-03-08", end="2026-03-08"):
    dialect = replace(get_engine(engine).sql, default_catalog="catalog_a")
    version = SimpleNamespace(dimensions=[], expression="SUM(amount)", time_column="observed_at", filters=None,
                              timezone=timezone, data_grain="day", time_format=None)
    asset = SimpleNamespace(catalog_name="catalog_a", db_name="schema_a", name="events")
    intent = Intent(metric_code="amount", start_date=date.fromisoformat(start), end_date=date.fromisoformat(end),
                    time_grain=grain)
    compiled = compile_metric_query(version=version, aliases={"b": asset}, steps=[], intent=intent, max_rows=100,
                                    time_type=data_type, dialect=dialect, bucket_grain=bucket)
    validate_sql(compiled.sql, dialect=dialect, allowed_tables={"catalog_a.schema_a.events"},
                 schema={"catalog_a.schema_a.events": {"amount", "observed_at"}}, max_rows=100)
    return sqlglot.parse_one(compiled.sql, read=dialect.name), compiled


@pytest.mark.parametrize("engine,data_type,local", [
    ("clickhouse", "DateTime('UTC')", "toTimeZone(b.observed_at, 'America/New_York')"),
    ("clickhouse", "Nullable(DateTime64(3, 'Asia/Tokyo'))", "toTimeZone(b.observed_at, 'America/New_York')"),
    ("oracle", "TIMESTAMP(6) WITH TIME ZONE", "CAST((b.observed_at AT TIME ZONE 'America/New_York') AS TIMESTAMP)"),
    ("oracle", "TIMESTAMP(6) WITH LOCAL TIME ZONE", "CAST((b.observed_at AT TIME ZONE 'America/New_York') AS TIMESTAMP)"),
    ("postgres", "timestamp(6) with time zone", "b.observed_at AT TIME ZONE 'America/New_York'"),
    ("postgres", "timestamptz", "b.observed_at AT TIME ZONE 'America/New_York'"),
])
@pytest.mark.parametrize("grain", ["day", "week", "month", "quarter", "year"])
def test_zoned_time_is_localized_before_every_period_operation(engine, data_type, local, grain):
    tree, compiled = compile_time(engine, data_type, grain=grain, bucket="day")
    # 用户期间与内部桶都要使用本地时间；SELECT/GROUP BY/ORDER BY 都保持相同表达式。
    assert local in compiled.sql
    assert "America/New_York" in tree.expressions[0].sql(dialect=get_engine(engine).sql.name)
    assert "America/New_York" in tree.expressions[1].sql(dialect=get_engine(engine).sql.name)
    for clause in ("group", "order"):
        assert all("America/New_York" in item.sql() for item in tree.args[clause].expressions)
    # 过滤直接比较原时间点与有类型的边界，不能先格式化列或把无偏移字符串交给数据库猜测。
    comparisons = list(tree.args["where"].find_all(exp.GTE, exp.LT))
    assert len(comparisons) == 2
    for comparison in comparisons:
        assert isinstance(comparison.this, exp.Column)
        assert not isinstance(comparison.expression, exp.Literal)


@pytest.mark.parametrize("engine,data_type,low,high", [
    ("clickhouse", "DateTime('UTC')", "2026-03-08 05:00:00", "2026-03-09 04:00:00"),
    ("oracle", "TIMESTAMP WITH TIME ZONE", "2026-03-08 00:00:00-05:00", "2026-03-09 00:00:00-04:00"),
    ("postgres", "timestamp with time zone", "2026-03-08 00:00:00-05:00", "2026-03-09 00:00:00-04:00"),
    ("sqlserver", "datetimeoffset(7)", "2026-03-08 00:00:00-05:00", "2026-03-09 00:00:00-04:00"),
])
def test_typed_boundaries_follow_spring_dst(engine, data_type, low, high):
    tree, compiled = compile_time(engine, data_type)
    bounds = {type(node): node.expression for node in tree.args["where"].find_all(exp.GTE, exp.LT)}
    assert low in bounds[exp.GTE].sql()
    assert high in bounds[exp.LT].sql()
    if engine == "clickhouse":
        assert "DateTime64(6, 'UTC')" in compiled.sql
    elif engine == "oracle":
        assert "TO_TIMESTAMP_TZ" in compiled.sql and "YYYY-MM-DD HH24:MI:SSTZH:TZM" in compiled.sql
    else:
        assert all(isinstance(bound, exp.Cast) and bound.to.this == exp.DataType.Type.TIMESTAMPTZ
                   for bound in bounds.values())


@pytest.mark.parametrize("engine,data_type,low,high", [
    ("clickhouse", "DateTime64(9, 'UTC')", "2026-11-01 04:00:00", "2026-11-02 05:00:00"),
    ("oracle", "TIMESTAMP WITH LOCAL TIME ZONE", "2026-11-01 00:00:00-04:00", "2026-11-02 00:00:00-05:00"),
    ("postgres", "timestamptz", "2026-11-01 00:00:00-04:00", "2026-11-02 00:00:00-05:00"),
    ("sqlserver", "datetimeoffset", "2026-11-01 00:00:00-04:00", "2026-11-02 00:00:00-05:00"),
])
def test_typed_boundaries_follow_autumn_dst(engine, data_type, low, high):
    tree, _ = compile_time(engine, data_type, start="2026-11-01", end="2026-11-01")
    bounds = {type(node): node.expression for node in tree.args["where"].find_all(exp.GTE, exp.LT)}
    assert low in bounds[exp.GTE].sql()
    assert high in bounds[exp.LT].sql()


@pytest.mark.parametrize("engine,data_type", [
    ("postgres", "timestamp(6) without time zone"), ("oracle", "TIMESTAMP(6)"),
    ("starrocks", "datetime"), ("doris", "datetime"), ("sqlserver", "datetime2(7)"),
    ("clickhouse", "Date32"),
])
def test_unzoned_columns_preserve_wall_clock_semantics(engine, data_type):
    _, compiled = compile_time(engine, data_type, grain="day", timezone=None)
    assert "AT TIME ZONE" not in compiled.sql and "toTimeZone" not in compiled.sql
    assert "2026-03-08" in compiled.sql


@pytest.mark.parametrize("engine,data_type", [
    ("clickhouse", "DateTime"), ("oracle", "TIMESTAMP WITH TIME ZONE"),
    ("postgres", "timestamptz"), ("sqlserver", "datetimeoffset"),
])
@pytest.mark.parametrize("timezone", [None, "Invalid/Timezone"])
def test_zoned_columns_require_valid_business_timezone(engine, data_type, timezone):
    with pytest.raises(Invalid, match="有效的业务时区"):
        compile_time(engine, data_type, timezone=timezone)


def test_fractional_hour_timezone_is_not_rounded():
    _, compiled = compile_time("postgres", "timestamptz", grain="day", timezone="Asia/Kathmandu")
    assert "2026-03-08 00:00:00+05:45" in compiled.sql
    assert "AT TIME ZONE 'Asia/Kathmandu'" in compiled.sql
