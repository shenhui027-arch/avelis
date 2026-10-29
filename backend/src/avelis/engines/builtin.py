"""内置引擎适配器。只登记在真实环境联调过的引擎与能力（StarRocks 3.5、MySQL 8）。

规划中的引擎（Doris、Hive、PostgreSQL、Oracle、SQL Server、DB2、ClickHouse）见 ADR 0004：
每个按同样的形状提供连接器、SQLGlot 方言与能力声明，联调通过后再登记。
"""

from __future__ import annotations

from avelis.connectors.fake import FakeConnector
from avelis.connectors.mysql import MySQLMetaConnector
from avelis.connectors.starrocks import INTERNAL_CATALOG, StarRocksConnector
from avelis.engines.base import EXTERNAL_TABLES, IDENTITY_TARGET, METADATA, QUERY, VIEW_LINEAGE, Engine
from avelis.lineage.sql import SqlDialect, strip_alias_prefixes

STARROCKS = Engine(
    kind="starrocks", label="StarRocks",
    # StarRocks 存储的视图定义把别名列写成 `db`.`alias`.`col`，解析前规整
    sql=SqlDialect(name="starrocks", default_catalog=INTERNAL_CATALOG, normalize=strip_alias_prefixes),
    name_levels=("catalog", "database", "table"),
    capabilities=frozenset({METADATA, VIEW_LINEAGE, QUERY, EXTERNAL_TABLES}),
    collector=StarRocksConnector, querier=StarRocksConnector, default_port=9030,
)

MYSQL = Engine(
    kind="mysql", label="MySQL",
    sql=SqlDialect(name="mysql", default_catalog="def"),  # information_schema 中 MySQL 的 Catalog 恒为 def
    name_levels=("database", "table"),
    capabilities=frozenset({METADATA, IDENTITY_TARGET}),
    collector=MySQLMetaConnector, default_port=3306,
    note="v0.1 只采元数据，作为外部表 / Catalog 表的追溯目标；视图解析与问数执行尚未联调",
)

FAKE = Engine(
    kind="fake", label="测试适配器",
    sql=SqlDialect(name="starrocks", default_catalog=INTERNAL_CATALOG, normalize=strip_alias_prefixes),
    name_levels=("catalog", "database", "table"),
    capabilities=frozenset({METADATA, VIEW_LINEAGE}),
    collector=lambda host, port, user, password, options: FakeConnector(options),
    is_test_adapter=True, needs_credentials=False, note="仅用于单元测试，结果会标记为测试数据",
)

BUILTIN = (STARROCKS, MYSQL, FAKE)
