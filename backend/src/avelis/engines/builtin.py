"""内置引擎适配器。只登记在真实环境联调过的引擎与能力。

已登记：StarRocks 3.5、MySQL 8、PostgreSQL 18、ClickHouse 26.8、SQL Server 2022、Oracle 23ai、Doris 4.1、
Hive 4.1（元数据与视图血缘）。只考虑 SQLGlot 支持的方言（ADR 0004）：
每个按同样的形状提供连接器、SQLGlot 方言与能力声明，联调通过后再登记。
"""

from __future__ import annotations

from avelis.connectors.clickhouse import CATALOG as CLICKHOUSE_CATALOG
from avelis.connectors.clickhouse import ClickHouseConnector
from avelis.connectors.doris import INTERNAL_CATALOG as DORIS_CATALOG
from avelis.connectors.doris import DorisConnector
from avelis.connectors.fake import FakeConnector
from avelis.connectors.hive import AUTH_MODES as HIVE_AUTH_MODES
from avelis.connectors.hive import CATALOG as HIVE_CATALOG
from avelis.connectors.hive import HiveConnector
from avelis.connectors.mysql import MySQLMetaConnector
from avelis.connectors.oracle import OracleConnector
from avelis.connectors.postgres import PostgresConnector
from avelis.connectors.sqlserver import SqlServerConnector
from avelis.connectors.starrocks import INTERNAL_CATALOG, StarRocksConnector
from avelis.engines.base import (
    DDL_AUDIT,
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
    kind="starrocks",
    label="StarRocks",
    script_separators=("DELIMITER",),
    # StarRocks 存储的视图定义把别名列写成 `db`.`alias`.`col`，解析前规整
    sql=SqlDialect(name="starrocks", default_catalog=INTERNAL_CATALOG, normalize=strip_alias_prefixes),
    name_levels=("catalog", "database", "table"),
    capabilities=frozenset({METADATA, VIEW_LINEAGE, QUERY, EXTERNAL_TABLES, LIVE_CHECK, DDL_AUDIT}),
    collector=StarRocksConnector,
    querier=StarRocksConnector,
    default_port=9030,
    options=(
        OptionField("databases", "采集的库", required=True),
        OptionField("catalogs", "Catalog", hint="只接 JDBC MySQL Catalog"),
        OptionField(
            "ddl_audit_table",
            "DDL 审计表",
            type="text",
            required=False,
            hint="库.表，默认表名 starrocks_audit_db__.starrocks_audit_tbl__；需要安装 AuditLoader 插件并授权采集账号 SELECT；留空表示未绑定审计表",
        ),
    ),
    note="2026-11-01 已实机验证 StarRocks 3.5.14 + AuditLoader 4.2.2；DDL 归因需绑定审计表并授予 SELECT，未绑定时不支持归因；"
    "FE 时区为 UTC，非 UTC 换算未实机验证；插件刚安装后的几十秒内可能未记录语句，此时归因为未匹配",
)

MYSQL = Engine(
    kind="mysql",
    label="MySQL",
    script_separators=("DELIMITER",),
    # information_schema 中的 Catalog 恒为 def；MySQL 查询只写 库.表
    sql=SqlDialect(name="mysql", default_catalog="def", catalog_in_sql=False),
    name_levels=("database", "table"),
    capabilities=frozenset({METADATA, IDENTITY_TARGET, VIEW_LINEAGE, QUERY, LIVE_CHECK, DDL_AUDIT}),
    collector=MySQLMetaConnector,
    querier=MySQLMetaConnector,
    default_port=3306,
    options=(
        OptionField("databases", "采集的库", required=True),
        OptionField(
            "ddl_audit_table",
            "DDL 审计表",
            type="text",
            required=False,
            hint="可选绑定库.表，如 mysql.general_log；通用日志需 general_log=ON 且 log_output 含 TABLE，采集账号需 SELECT；兼容审计表需 event_time（TIMESTAMP）、user_host、command_type、argument",
        ),
    ),
    note="采集账号需元数据可见性与 SHOW VIEW；问数使用专用只读账号（SELECT 与 SHOW VIEW）；server_uuid 确认实例身份；"
    "不报告数据新鲜度；2026-11-01 已实机验证 MySQL 8.4.10；DDL 归因需绑定审计表，未绑定时不支持归因；"
    "general_log 实测也记录执行失败的 DDL，只作候选证据，与结构变化交叉核对；审计时间统一按 UTC 读取",
)

POSTGRES = Engine(
    kind="postgres",
    label="PostgreSQL",
    sql=SqlDialect(name="postgres", default_catalog="postgres"),
    name_levels=("database", "schema", "table"),
    catalog_option="database",
    capabilities=frozenset({METADATA, VIEW_LINEAGE, QUERY, LIVE_CHECK, DDL_AUDIT}),
    collector=PostgresConnector,
    querier=PostgresConnector,
    default_port=5432,
    options=(
        OptionField("database", "库", type="text", required=True),
        OptionField("schemas", "采集的模式", required=True),
    ),
    note="采集账号只读系统目录；物理身份用 pg_class.oid；不报告数据新鲜度；"
    "DDL 归因需在库中部署事件触发器（avelis_audit.ddl_event），采集账号可读该表",
)

CLICKHOUSE = Engine(
    kind="clickhouse",
    label="ClickHouse",
    # 库.表 两级；资产名补固定 Catalog 段，生成的 SQL 不写 Catalog 段
    sql=SqlDialect(name="clickhouse", default_catalog=CLICKHOUSE_CATALOG, catalog_in_sql=False),
    name_levels=("database", "table"),
    capabilities=frozenset({METADATA, VIEW_LINEAGE, QUERY, LIVE_CHECK, DDL_AUDIT}),
    collector=ClickHouseConnector,
    querier=ClickHouseConnector,
    default_port=8123,
    options=(OptionField("databases", "采集的库", required=True),),
    note="HTTP 接口；排序键不保证唯一，不作为粒度依据；查询账号需 readonly=2；"
    "DDL 归因读 system.query_log（采集账号需其 SELECT）",
)

SQLSERVER = Engine(
    kind="sqlserver",
    label="SQL Server",
    script_separators=("GO",),
    sql=SqlDialect(name="tsql", default_catalog="master", national_strings=True),
    name_levels=("database", "schema", "table"),
    catalog_option="database",
    capabilities=frozenset({METADATA, VIEW_LINEAGE, QUERY, LIVE_CHECK, DDL_AUDIT}),
    collector=SqlServerConnector,
    querier=SqlServerConnector,
    default_port=1433,
    options=(
        OptionField("database", "库", type="text", required=True),
        OptionField("schemas", "采集的模式", required=True),
    ),
    note="采集账号需模式上的 VIEW DEFINITION；查询账号需 SELECT 与 VIEW DEFINITION；object_id 作物理身份；不报告数据新鲜度；"
    "2026-11-01 已实机验证 SQL Server 2022 的 DDL 归因；需启用默认跟踪，并授予 ALTER TRACE（相关读取若需 VIEW SERVER STATE 也须授权）；"
    "只采用已提交的对象级事件；当前目录恢复的模式仅作候选，无法确证历史模式或点名列时不确定归因；"
    "StartTime 按服务器本地时间换算 UTC，使用服务器当前偏移；演示实例偏移为 0，本地时间换 UTC 与跨夏令时未实机验证，跨夏令时可能不准确",
)

ORACLE = Engine(
    kind="oracle",
    label="Oracle",
    script_separators=("/",),
    # 服务名作 catalog 段，查询中不写；DATE 带时间部分，按日期过滤用区间
    sql=SqlDialect(
        name="oracle",
        default_catalog="FREEPDB1",
        normalize=fold_identifiers("oracle"),
        catalog_in_sql=False,
        date_has_time=True,
    ),
    name_levels=("service", "schema", "table"),
    catalog_option="service",
    capabilities=frozenset({METADATA, VIEW_LINEAGE, QUERY, LIVE_CHECK, DDL_AUDIT}),
    collector=OracleConnector,
    querier=OracleConnector,
    default_port=1521,
    options=(
        OptionField("service", "服务名", type="text", required=True, hint="如 FREEPDB1"),
        OptionField("schemas", "采集的模式", required=True, hint="模式名按大写保存"),
    ),
    note="采集账号需 SELECT_CATALOG_ROLE；OBJECT_ID 作物理身份；标识符按大写保存；不报告数据新鲜度；"
    "2026-11-01 已实机验证 Oracle 23ai Free 的 DDL 归因，使用自建统一审计策略（CREATE / ALTER / DROP TABLE、CREATE / DROP VIEW、COMMENT、RENAME、TRUNCATE TABLE）与 AUDIT_VIEWER；"
    "只采用 RETURN_CODE = 0 的成功记录；优先 EVENT_TIMESTAMP_UTC，旧版 EVENT_TIMESTAMP 在 UTC 审计会话中读取；"
    "无记录且无策略的 not_enabled 分支未实机验证",
)

DORIS = Engine(
    kind="doris",
    label="Doris",
    script_separators=("DELIMITER",),
    # 视图定义的存储写法与 StarRocks 相同（`internal`.`db`.`alias`.`col`），复用别名前缀规整
    sql=SqlDialect(name="doris", default_catalog=DORIS_CATALOG, normalize=strip_alias_prefixes),
    name_levels=("catalog", "database", "table"),
    capabilities=frozenset({METADATA, VIEW_LINEAGE, QUERY, LIVE_CHECK, DDL_AUDIT}),
    collector=DorisConnector,
    querier=DorisConnector,
    default_port=9030,
    options=(OptionField("databases", "采集的库", required=True),),
    note="采集账号只需 SHOW_VIEW_PRIV；查询账号需 SELECT_PRIV 与 SHOW_VIEW_PRIV；TABLE_ID 作物理身份（视图也有）；"
    "外部 Catalog 尚未联调；DDL 归因读 __internal_schema.audit_log（采集账号需其 SELECT_PRIV）",
)

HIVE = Engine(
    kind="hive",
    label="Hive",
    # 库.表 两级；资产名补固定 Catalog 段 hive，生成的 SQL 不写 Catalog 段
    sql=SqlDialect(name="hive", default_catalog=HIVE_CATALOG, catalog_in_sql=False),
    name_levels=("database", "table"),
    capabilities=frozenset({METADATA, VIEW_LINEAGE}),
    collector=HiveConnector,
    default_port=10000,
    options=(
        OptionField("databases", "采集的库", required=True),
        OptionField(
            "auth",
            "认证方式",
            type="choice",
            choices=HIVE_AUTH_MODES,
            default="NONE",
            hint="NONE 不校验口令，账号可不填；LDAP 需账号与口令；Kerberos 未支持",
        ),
    ),
    credential_free=("auth", ("NONE", "NOSASL")),
    note="经 HiveServer2（binary 传输）只读元数据，不读业务数据；视图定义取 Hive 保存的展开文本解析血缘；"
    "不支持问数执行；HiveServer2 不暴露表 ID，表与视图按名称对应；主键约束不校验，不作粒度依据",
)

FAKE = Engine(
    kind="fake",
    label="测试适配器",
    script_separators=("DELIMITER",),
    sql=SqlDialect(name="starrocks", default_catalog=INTERNAL_CATALOG, normalize=strip_alias_prefixes),
    name_levels=("catalog", "database", "table"),
    capabilities=frozenset({METADATA, VIEW_LINEAGE}),
    collector=lambda host, port, user, password, options: FakeConnector(options),
    is_test_adapter=True,
    needs_credentials=False,
    note="仅用于单元测试，结果会标记为测试数据",
)

BUILTIN = (STARROCKS, MYSQL, POSTGRES, CLICKHOUSE, SQLSERVER, ORACLE, DORIS, HIVE, FAKE)
