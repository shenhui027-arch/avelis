"""引擎适配层：核心模块只经注册表取方言与能力，不按数据源类型分支。"""

from __future__ import annotations

import pytest

from avelis.ask.compiler import distinct_values_sql, duplicate_key_sql, validate_sql
from avelis.engines import engines, get_engine, query_connector, register
from avelis.engines.base import LIVE_CHECK, QUERY, VIEW_LINEAGE, CapabilityMissing, Engine, UnsupportedEngine
from avelis.lineage.sql import SqlDialect, canonical_hash_of, parse_query
from avelis.models import Datasource


def test_registry_lists_only_integrated_engines():
    kinds = {e.kind for e in engines()}
    assert kinds == {"starrocks", "mysql", "postgres", "clickhouse", "sqlserver", "oracle", "doris", "hive", "fake"}
    with pytest.raises(UnsupportedEngine, match="db2"):
        get_engine("db2")
    with pytest.raises(ValueError, match="已登记"):
        register(get_engine("mysql"))


def test_capabilities_are_declared_not_guessed():
    sr, my = get_engine("starrocks"), get_engine("mysql")
    assert sr.supports(QUERY) and sr.supports(VIEW_LINEAGE)
    assert not my.supports(QUERY) and not my.supports(VIEW_LINEAGE)
    pg = get_engine("postgres")
    assert pg.supports(QUERY) and pg.supports(VIEW_LINEAGE) and pg.supports(LIVE_CHECK)
    # catalog 段因数据源而异：PostgreSQL 用配置的库名
    assert pg.dialect_for({"database": "storedb"}).default_catalog == "storedb"
    assert get_engine("starrocks").dialect_for({"database": "x"}).default_catalog == "default_catalog"
    # 执行前核对需要只读执行账号：只有能执行问数的引擎才声明
    assert sr.supports(LIVE_CHECK) and not my.supports(LIVE_CHECK) and not get_engine("fake").supports(LIVE_CHECK)
    ds = Datasource(name="维表库", kind="mysql", host="127.0.0.1", port=3306, options={})
    with pytest.raises(CapabilityMissing, match="维表库（MySQL）不支持问数执行"):
        query_connector(None, ds)  # 不触达数据库、不读口令


def test_generated_sql_follows_dialect():
    # 引用符随方言变化：StarRocks / MySQL 用反引号，PostgreSQL 风格用双引号
    assert distinct_values_sql("default_catalog.shop.orders", "region", limit=200, dialect="starrocks") == (
        "SELECT DISTINCT `t`.`region` FROM `default_catalog`.`shop`.`orders` AS `t` "
        "WHERE NOT `t`.`region` IS NULL LIMIT 200")
    pg = duplicate_key_sql("analytics.public.orders", ["order_id"], dialect="postgres")
    assert pg == ('SELECT "t"."order_id", COUNT(*) AS "n" FROM "analytics"."public"."orders" AS "t" '
                  'GROUP BY "t"."order_id" HAVING COUNT(*) > 1 LIMIT 1')
    validate_sql(pg, dialect="postgres", allowed_tables={"analytics.public.orders"},
                 schema={"analytics.public.orders": {"order_id"}}, max_rows=1)


def test_two_level_engine_sql_omits_catalog():
    # ClickHouse：资产名补固定 Catalog 段，生成的 SQL 只写 库.表；校验时按方言补回 Catalog 段再比对
    ch = get_engine("clickhouse").dialect_for({"databases": ["store"]})
    assert not ch.catalog_in_sql and ch.default_catalog == "clickhouse"
    sql = duplicate_key_sql("clickhouse.store.returns", ["sale_id"], dialect=ch)
    assert 'FROM "store"."returns" AS "t"' in sql and "clickhouse" not in sql
    validate_sql(sql, dialect=ch, allowed_tables={"clickhouse.store.returns"},
                 schema={"clickhouse.store.returns": {"sale_id"}}, max_rows=1)
    from avelis.ask.compiler import ValidationFailed

    with pytest.raises(ValidationFailed, match="未批准的表 clickhouse.store.sales"):
        validate_sql('SELECT "t"."sale_id" FROM "store"."sales" AS "t" LIMIT 1', dialect=ch,
                     allowed_tables={"clickhouse.store.returns"}, schema={}, max_rows=1)


def test_clickhouse_types_and_identity():
    from avelis.connectors.clickhouse import _identity, _kind, _type

    assert _type("Nullable(Date)") == ("Date", True) and _type("Decimal(18, 2)") == ("Decimal(18, 2)", False)
    assert _kind("View") == "view" and _kind("ReplacingMergeTree") == "table"
    # 视图不用 UUID 作身份：CREATE OR REPLACE VIEW 会换新 UUID
    assert _identity("b91a2ade-ffaa-41ca-9e03-49a0bf4c9bd7", "view") is None
    assert _identity("00000000-0000-0000-0000-000000000000", "table") is None
    assert _identity("42a74fc4-e8dd-4333-9640-0c9875fc54ff", "table") == "42a74fc4-e8dd-4333-9640-0c9875fc54ff"


def test_sqlserver_names_types_and_unicode_literals():
    from avelis.ask.intent import Intent
    from avelis.connectors.sqlserver import type_name, view_query

    assert type_name("nvarchar", 32, 0, 0) == "nvarchar(16)" and type_name("varchar", -1, 0, 0) == "varchar(max)"
    assert type_name("decimal", 9, 18, 2) == "decimal(18,2)" and type_name("int", 4, 10, 0) == "int"
    # sys.sql_modules 存整条 CREATE VIEW：只取查询部分
    assert view_query("CREATE VIEW store.v AS\nSELECT a FROM store.t WHERE b = N'x'") == \
        "SELECT a FROM store.t WHERE b = N'x'"
    ms = get_engine("sqlserver").dialect_for({"database": "storedb"})
    assert ms.default_catalog == "storedb" and ms.national_strings
    from datetime import date
    from types import SimpleNamespace

    from avelis.ask.compiler import compile_metric_query
    from avelis.models import Asset

    base = Asset(catalog_name="storedb", db_name="store", name="city_daily_sales")
    version = SimpleNamespace(dimensions=[{"name": "city", "column": "city"}], expression="SUM(sales_amount)",
                              time_column="sale_date", filters=None)
    intent = Intent(metric_code="m", start_date=date(2026, 9, 15), end_date=date(2026, 9, 15),
                    filters=[{"dimension": "city", "values": ["上海"]}], group_by=[])
    sql = compile_metric_query(version=version, aliases={"b": base}, steps=[], intent=intent, max_rows=1000,
                               time_type="date", dialect=ms).sql
    # NVARCHAR 列在非 UTF-8 排序规则下，中文只有写成 N'...' 才能正确比较；行数上限写成 TOP
    assert sql.startswith("SELECT TOP 1001 ") and "b.city = N'上海'" in sql  # 多 1 行用于判断截断
    assert "FROM storedb.store.city_daily_sales AS b" in sql


def test_parser_core_is_engine_agnostic():
    # 解析核心只认 SqlDialect：同一模块可按 库.模式.表 三级命名解析 PostgreSQL 写法（仅验证解析层，不代表已支持该引擎）
    pg = SqlDialect(name="postgres", default_catalog="analytics")
    schema = {"analytics": {"public": {"orders": {"order_id": "INT", "amount": "NUMERIC", "day": "DATE"}}}}
    parsed = parse_query('SELECT "day", SUM(amount) AS total FROM public.orders GROUP BY "day"', dialect=pg,
                         schema=schema)
    assert parsed.status == "ok" and parsed.tables == ["analytics.public.orders"]
    total = next(c for c in parsed.columns if c.name == "total")
    assert total.relation == "value_aggregated" and total.sources[0].ref.table == "analytics.public.orders"
    assert parsed.group_outputs == ["day"]
    # 引擎特有的规整只在该引擎的方言里生效
    sr = get_engine("starrocks").sql
    alias_prefixed = "SELECT `shop`.`o`.`id` FROM `shop`.`orders` `o`"
    plain = "SELECT `o`.`id` FROM `shop`.`orders` `o`"
    assert canonical_hash_of(alias_prefixed, sr)[1] == canonical_hash_of(plain, sr)[1]
    bare = SqlDialect(name="starrocks", default_catalog="default_catalog")
    assert canonical_hash_of(alias_prefixed, bare)[1] != canonical_hash_of(plain, bare)[1]


def test_engine_description_is_serialisable():
    d = get_engine("starrocks").describe()
    assert d["dialect"] == "starrocks" and "query" in d["capabilities"] and d["name_levels"][0] == "catalog"
    assert isinstance(Engine, type)


def test_model_history_drops_result_values_when_not_sent():
    from langchain_core.messages import AIMessage, HumanMessage

    from avelis.ask.agent import _history_for_model

    old = AIMessage("[answered] 2026-09-15（region=华东） 净销售额（V1）：270.00")  # 旧检查点：没有标记
    new = AIMessage("[answered] 净销售额：270.00", additional_kwargs={
        "avelis_summary": True, "redacted": "[answered] · 指标 net_sales · 日期 2026-09-15（结果未发送给模型）"})
    history = [HumanMessage("2026-09-15 华东区净销售额"), old, new]
    sent = _history_for_model(history, send_results=False)
    assert "270" not in " ".join(str(m.content) for m in sent)
    assert "指标 net_sales" in str(sent[2].content) and str(sent[1].content).startswith("[answered]")
    assert _history_for_model(history, send_results=True) == history


def test_oracle_case_folding_dates_and_fetch_first():
    from datetime import date
    from types import SimpleNamespace

    from avelis.ask.compiler import compile_metric_query
    from avelis.ask.intent import Intent
    from avelis.connectors.oracle import type_name
    from avelis.models import Asset

    assert type_name("VARCHAR2", 64, None, None, 16) == "VARCHAR2(16)"
    assert type_name("NUMBER", 22, 18, 2, None) == "NUMBER(18,2)" and type_name("DATE", 7, None, None, None) == "DATE"
    ora = get_engine("oracle").dialect_for({"service": "FREEPDB1", "schemas": ["STORE"]})
    assert ora.default_catalog == "FREEPDB1" and not ora.catalog_in_sql and ora.date_has_time
    # 视图原文的 store.sales 在数据字典中是 STORE.SALES：先按 Oracle 规则折叠再对资产
    schema = {"FREEPDB1": {"STORE": {"SALES": {"AMOUNT": "NUMBER", "SALE_DATE": "DATE"}}}}
    parsed = parse_query("SELECT sale_date, SUM(amount) AS total FROM store.sales GROUP BY sale_date",
                         dialect=ora, schema=schema)
    assert parsed.status == "ok" and parsed.tables == ["FREEPDB1.STORE.SALES"]
    base = Asset(catalog_name="FREEPDB1", db_name="STORE", name="SALES")
    version = SimpleNamespace(dimensions=[], expression="SUM(AMOUNT)", time_column="SALE_DATE", filters=None)
    intent = Intent(metric_code="m", start_date=date(2026, 9, 15), end_date=date(2026, 9, 15))
    compiled = compile_metric_query(version=version, aliases={"b": base}, steps=[], intent=intent, max_rows=1000,
                                    time_type="date", dialect=ora)
    # DATE 带时间部分：用 [当日 0 点, 次日 0 点) 区间；不写服务名；行数上限为 FETCH FIRST
    assert "b.SALE_DATE >= '2026-09-15 00:00:00' AND b.SALE_DATE < '2026-09-16 00:00:00'" in compiled.sql
    assert "FROM STORE.SALES b" in compiled.sql and compiled.sql.endswith("FETCH FIRST 1001 ROWS ONLY")
    validate_sql(compiled.sql, dialect=ora, allowed_tables={"FREEPDB1.STORE.SALES"},
                 schema={"FREEPDB1.STORE.SALES": {"AMOUNT", "SALE_DATE"}}, max_rows=1000)


def test_count_star_is_not_select_star():
    # COUNT(*) 不是未展开的 SELECT *：视图列的血缘应完整（此前被误判为 partial）
    sr = get_engine("starrocks").sql
    schema = {"default_catalog": {"shop": {"orders": {"store_id": "INT", "order_date": "DATE", "paid_amount": "DECIMAL"}}}}
    parsed = parse_query("SELECT o.store_id, COUNT(*) AS n, SUM(o.paid_amount) AS amt FROM shop.orders o "
                         "GROUP BY o.store_id", dialect=sr, schema=schema)
    assert parsed.status == "ok", parsed.reason
    star = parse_query("SELECT * FROM shop.missing", dialect=sr, schema=schema)
    assert star.status != "ok"


def test_time_grain_column_does_not_collide_with_period_dimension():
    """口径开放了名为 period 的维度时，按日分组生成的时间段列换名，不覆盖维度也不丢日期（审计 #17）。"""
    from datetime import date
    from types import SimpleNamespace

    from avelis.ask.compiler import compile_metric_query
    from avelis.ask.intent import Intent, period_column
    from avelis.models import Asset

    base = Asset(catalog_name="default_catalog", db_name="shop", name="orders")
    version = SimpleNamespace(dimensions=[{"name": "period", "column": "season"}], expression="SUM(amount)",
                              time_column="order_date", filters=None)
    intent = Intent(metric_code="m", start_date=date(2026, 9, 1), end_date=date(2026, 9, 30), group_by=["period"],
                    time_grain="day")
    compiled = compile_metric_query(version=version, aliases={"b": base}, steps=[], intent=intent, max_rows=1000,
                                    time_type="date", dialect=get_engine("starrocks").sql)
    assert compiled.columns == ["时间段", "period", "value"] and len(set(compiled.columns)) == 3
    assert "AS `时间段`" in compiled.sql and "b.season AS period" in compiled.sql
    assert period_column([]) == "period" and period_column(["period", "时间段"]) == "period_time"


def test_time_column_avoids_metric_display_name_and_probe_row():
    """指标显示名为 period 时，时间段列也换名；不限行数时多取 1 行用于判断截断，校验允许这 1 行（复核 #8、#9）。"""
    from datetime import date
    from types import SimpleNamespace

    from avelis.ask.compiler import ValidationFailed, compile_metric_query, validate_sql
    from avelis.ask.intent import Intent
    from avelis.models import Asset

    base = Asset(catalog_name="default_catalog", db_name="shop", name="orders")
    version = SimpleNamespace(dimensions=[], expression="SUM(amount)", time_column="order_date", filters=None)
    intent = Intent(metric_code="m", start_date=date(2026, 9, 1), end_date=date(2026, 9, 30), time_grain="day")
    sr = get_engine("starrocks").sql
    compiled = compile_metric_query(version=version, aliases={"b": base}, steps=[], intent=intent, max_rows=1000,
                                    time_type="date", dialect=sr, reserved_names=("period", "value"))
    assert compiled.columns[0] == "时间段" and compiled.sql.endswith("LIMIT 1001")
    schema = {"default_catalog.shop.orders": {"amount", "order_date"}}
    validate_sql(compiled.sql, dialect=sr, allowed_tables={"default_catalog.shop.orders"}, schema=schema, max_rows=1000)
    with pytest.raises(ValidationFailed):
        validate_sql(compiled.sql.replace("LIMIT 1001", "LIMIT 1002"), dialect=sr,
                     allowed_tables={"default_catalog.shop.orders"}, schema=schema, max_rows=1000)
    limited = Intent(metric_code="m", start_date=date(2026, 9, 1), end_date=date(2026, 9, 30), limit=10)
    assert compile_metric_query(version=version, aliases={"b": base}, steps=[], intent=limited, max_rows=1000,
                                time_type="date", dialect=sr).sql.endswith("LIMIT 10")
    # 请求的 limit 超过系统上限：同样多取 1 行，连接器据此标记截断（复核：limit=10、上限 5 时曾只取 5 行而不标截断）
    over = compile_metric_query(version=version, aliases={"b": base}, steps=[], intent=limited, max_rows=5,
                                time_type="date", dialect=sr).sql
    assert over.endswith("LIMIT 6")
    validate_sql(over, dialect=sr, allowed_tables={"default_catalog.shop.orders"}, schema=schema, max_rows=5)
