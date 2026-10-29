"""内置引擎适配器。只登记在真实环境联调过的引擎与能力。

已登记：StarRocks 3.5、MySQL 8（只采元数据）、PostgreSQL 18、ClickHouse 26.8、SQL Server 2022、Oracle 23ai、Doris 4.1。
规划中的引擎（Hive；只考虑 SQLGlot 支持的方言）见 ADR 0004：
每个按同样的形状提供连接器、SQLGlot 方言与能力声明，联调通过后再登记。
"""

from __future__ import annotations

from avelis.connectors.clickhouse import CATALOG as CLICKHOUSE_CATALOG
from avelis.connectors.clickhouse import ClickHouseConnector
from avelis.connectors.doris import INTERNAL_CATALOG as DORIS_CATALOG
from avelis.connectors.doris import DorisConnector
from avelis.connectors.fake import FakeConnector
from avelis.connectors.mysql import MySQLMetaConnector
from avelis.connectors.oracle import OracleConnector
from avelis.connectors.postgres import PostgresConnector
from avelis.connectors.sqlserver import SqlServerConnector
from avelis.connectors.starrocks import INTERNAL_CATALOG, StarRocksConnector
from avelis.engines.base import (
    EXTERNAL_TABLES,
    IDENTITY_TARGET,
    LIVE_CHECK,
    METADATA,
    QUERY,
    VIEW_LINEAGE,
    Engine,
    OptionField,
)
from avelis.lineage.sql import SqlDialect, fold_identifiers, strip_alias_prefixes

STARROCKS = Engine(
    kind="starrocks", label="StarRocks",
    # StarRocks 存储的视图定义把别名列写成 `db`.`alias`.`col`，解析前规整
    sql=SqlDialect(name="starrocks", default_catalog=INTERNAL_CATALOG, normalize=strip_alias_prefixes),
    name_levels=("catalog", "database", "table"),
    capabilities=frozenset({METADATA, VIEW_LINEAGE, QUERY, EXTERNAL_TABLES, LIVE_CHECK}),
    collector=StarRocksConnector, querier=StarRocksConnector, default_port=9030,
    options=(OptionField("databases", "采集的库", required=True),
             OptionField("catalogs", "Catalog", hint="只接 JDBC MySQL Catalog")),
)

MYSQL = Engine(
    kind="mysql", label="MySQL",
    sql=SqlDialect(name="mysql", default_catalog="def"),  # information_schema 中 MySQL 的 Catalog 恒为 def
    name_levels=("database", "table"),
    capabilities=frozenset({METADATA, IDENTITY_TARGET}),
    collector=MySQLMetaConnector, default_port=3306,
    options=(OptionField("databases", "采集的库", required=True),),
    note="v0.1 只采元数据，作为外部表 / Catalog 表的追溯目标；视图解析与问数执行尚未联调",
)

POSTGRES = Engine(
    kind="postgres", label="PostgreSQL",
    sql=SqlDialect(name="postgres", default_catalog="postgres"),
    name_levels=("database", "schema", "table"), catalog_option="database",
    capabilities=frozenset({METADATA, VIEW_LINEAGE, QUERY, LIVE_CHECK}),
    collector=PostgresConnector, querier=PostgresConnector, default_port=5432,
    options=(OptionField("database", "库", type="text", required=True),
             OptionField("schemas", "采集的模式", required=True)),
    note="采集账号只读系统目录；物理身份用 pg_class.oid；不报告数据新鲜度",
)

CLICKHOUSE = Engine(
    kind="clickhouse", label="ClickHouse",
    # 库.表 两级；资产名补固定 Catalog 段，生成的 SQL 不写 Catalog 段
    sql=SqlDialect(name="clickhouse", default_catalog=CLICKHOUSE_CATALOG, catalog_in_sql=False),
    name_levels=("database", "table"),
    capabilities=frozenset({METADATA, VIEW_LINEAGE, QUERY, LIVE_CHECK}),
    collector=ClickHouseConnector, querier=ClickHouseConnector, default_port=8123,
    options=(OptionField("databases", "采集的库", required=True),),
    note="HTTP 接口；排序键不保证唯一，不作为粒度依据；查询账号需 readonly=2",
)

SQLSERVER = Engine(
    kind="sqlserver", label="SQL Server",
    sql=SqlDialect(name="tsql", default_catalog="master", national_strings=True),
    name_levels=("database", "schema", "table"), catalog_option="database",
    capabilities=frozenset({METADATA, VIEW_LINEAGE, QUERY, LIVE_CHECK}),
    collector=SqlServerConnector, querier=SqlServerConnector, default_port=1433,
    options=(OptionField("database", "库", type="text", required=True),
             OptionField("schemas", "采集的模式", required=True)),
    note="采集账号需模式上的 VIEW DEFINITION；查询账号需 SELECT 与 VIEW DEFINITION；object_id 作物理身份；不报告数据新鲜度",
)

ORACLE = Engine(
    kind="oracle", label="Oracle",
    # 服务名作 catalog 段，查询中不写；DATE 带时间部分，按日期过滤用区间
    sql=SqlDialect(name="oracle", default_catalog="FREEPDB1", normalize=fold_identifiers("oracle"),
                   catalog_in_sql=False, date_has_time=True),
    name_levels=("service", "schema", "table"), catalog_option="service",
    capabilities=frozenset({METADATA, VIEW_LINEAGE, QUERY, LIVE_CHECK}),
    collector=OracleConnector, querier=OracleConnector, default_port=1521,
    options=(OptionField("service", "服务名", type="text", required=True, hint="如 FREEPDB1"),
             OptionField("schemas", "采集的模式", required=True, hint="模式名按大写保存")),
    note="采集账号需 SELECT_CATALOG_ROLE；OBJECT_ID 作物理身份；标识符按大写保存；不报告数据新鲜度",
)

DORIS = Engine(
    kind="doris", label="Doris",
    # 视图定义的存储写法与 StarRocks 相同（`internal`.`db`.`alias`.`col`），复用别名前缀规整
    sql=SqlDialect(name="doris", default_catalog=DORIS_CATALOG, normalize=strip_alias_prefixes),
    name_levels=("catalog", "database", "table"),
    capabilities=frozenset({METADATA, VIEW_LINEAGE, QUERY, LIVE_CHECK}),
    collector=DorisConnector, querier=DorisConnector, default_port=9030,
    options=(OptionField("databases", "采集的库", required=True),),
    note="采集账号只需 SHOW_VIEW_PRIV；查询账号需 SELECT_PRIV 与 SHOW_VIEW_PRIV；TABLE_ID 作物理身份（视图也有）；"
         "外部 Catalog 尚未联调",
)

FAKE = Engine(
    kind="fake", label="测试适配器",
    sql=SqlDialect(name="starrocks", default_catalog=INTERNAL_CATALOG, normalize=strip_alias_prefixes),
    name_levels=("catalog", "database", "table"),
    capabilities=frozenset({METADATA, VIEW_LINEAGE}),
    collector=lambda host, port, user, password, options: FakeConnector(options),
    is_test_adapter=True, needs_credentials=False, note="仅用于单元测试，结果会标记为测试数据",
)

BUILTIN = (STARROCKS, MYSQL, POSTGRES, CLICKHOUSE, SQLSERVER, ORACLE, DORIS, FAKE)
