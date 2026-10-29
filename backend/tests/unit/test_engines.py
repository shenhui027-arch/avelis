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
    assert kinds == {"starrocks", "mysql", "postgres", "fake"}
    with pytest.raises(UnsupportedEngine, match="oracle"):
        get_engine("oracle")
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
