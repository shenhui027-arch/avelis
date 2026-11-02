# StarRocks 3.5.14 实机核验记录

- 环境：本机 Docker，`starrocks/allin1-ubuntu:3.5.14`（`3.5.14-23a56ec`，单 FE + 单 BE），电商维表库 MySQL 8.4.10，见 [ADR 0001](../decisions/0001-demo-environment.md)。
- 方法：`uv run scripts/demo.py verify` 逐项真实执行（`scripts/sr_probe.py`），原始结果写入 `.demo/sr-verify.json`（不进仓库，口令已替换为 `<redacted>`）。破坏性试验只在临时库 `avelis_probe` 中进行。
- 日期：元数据核验为 2026-10-29；DDL 审计归因补充验证为 2026-11-01，使用 StarRocks 3.5.14 + AuditLoader 4.2.2。
- 适用范围：以下结论只代表单节点 Docker 环境；多 FE/BE、存算分离、其他版本、生产权限体系仍待验证。

## 结论速览

| # | 问题 | 实测结论 | 对 Avelis 的影响 |
|---|---|---|---|
| 1 | 视图定义从哪取、长什么样 | `SHOW CREATE VIEW` 与 `information_schema.views.VIEW_DEFINITION` 都返回 StarRocks 规范化后的 SQL：全限定、反引号；前者另含列清单、`COMMENT` 和 `SECURITY NONE` | 以 `VIEW_DEFINITION` 为定义原文；规范化指纹与建视图时的原始 SQL **完全一致**，只改格式不会误报定义变化 |
| 2 | 带别名的视图定义 | 列被写成 `` `db`.`alias`.`col` ``，Catalog 表写成 `` `catalog`.`db`.`alias`.`col` `` | SQLGlot 会把别名当表名、把库名当前缀。解析前必须做一次“别名列去前缀”归一化；归一化后列级血缘正确（已验证） |
| 3 | 表的物理身份 | 内表在 `information_schema.tables_config.TABLE_ID` 有 ID（采集账号可读）；**改名 ID 不变**（10325 → 10325），**删除后同名重建 ID 变化**（→ 10331） | 内表身份用 `TABLE_ID`，改名与重建可准确区分 |
| 4 | 视图、外部表的身份 | `tables_config.TABLE_ID` 为 0；真实 ID 只在 `SHOW PROC '/dbs/<id>'` 中，需要 `SYSTEM OPERATE` 权限 | 默认不给采集账号 OPERATE。视图与外部表身份退回“库 + 名称 + `CREATE_TIME`（秒级）+ 定义哈希”；可选：管理员授予 OPERATE 后启用精确 ID |
| 5 | 集群身份 | `SHOW FRONTENDS` 返回 `ClusterId`（574017141），但需要 `OPERATE` 或 `NODE` | 集群身份默认用配置的 FE 端点；有权限时记录 ClusterId |
| 6 | 表模型与键 | `tables_config.TABLE_MODEL`：`PRIMARY_KEYS`（含 `PRIMARY_KEY` 列）、`DUP_KEYS`（无唯一性） | 主键表的粒度可直接“观察”；明细模型表的粒度必须声明并核验 |
| 7 | 数据新鲜度 | `information_schema.partitions_meta.VISIBLE_VERSION_TIME` 按分区给出最近可见版本时间；`tables.UPDATE_TIME` 给出表级时间；`ROW_COUNT` 在导入后短时间内仍为 0 | 新鲜度用 `VISIBLE_VERSION_TIME`；不用 `ROW_COUNT` 判断数据量 |
| 8 | 依赖关系表 | `sys.object_dependencies` 对普通视图为空（只覆盖物化视图） | 视图依赖必须来自 SQL 解析 |
| 9 | 删除被视图引用的列 | `ALTER TABLE … DROP COLUMN` **不检查**视图依赖，直接成功；视图仍在，`SHOW CREATE VIEW` 照常返回旧定义；查询视图报 1064：“references invalid table(s) or column(s)… Column … cannot be resolved” | StarRocks 不会阻止破坏性变更。Avelis 必须靠采集 + 列级血缘发现“上游列消失”，把视图及其下游标为受影响（验收故事 7） |
| 10 | JDBC Catalog 元数据 | `SHOW CATALOGS`、`SHOW CREATE CATALOG`、`SHOW DATABASES FROM`、`SHOW TABLES FROM`、`<catalog>.information_schema.tables` 都可用；`SHOW CREATE CATALOG` **不显示口令**，给出 `user`、`jdbc_uri`、`driver_url`、`driver_class`、驱动 `checksum` | 外部表身份匹配从 `jdbc_uri` 取端点；凭据不会被采集到 |
| 11 | 视图能否引用 Catalog 表 | 可以（`ads.sales_by_category` 关联 MySQL 维表），查询结果正确 | 跨库血缘可以直接从视图定义解析 |
| 12 | `ENGINE=mysql` 外部表 | 3.5.14 仍支持；但 BE 自带的 MySQL 客户端**加载不了** `caching_sha2_password`，MySQL 8.4 默认账号连接失败：“Plugin caching_sha2_password could not be loaded”。改用 `mysql_native_password` 账号（服务端需 `--mysql-native-password=ON`）后正常 | 采集要同时识别两种外部表；旧式外部表的 `SHOW CREATE TABLE` 口令显示为空串，`host`、`port`、`database`、`table` 可用于身份匹配；`information_schema.tables.ENGINE = 'MySQL'`；列注释丢失 |
| 13 | 只读账号边界 | `INSERT`、`DROP TABLE`、`CREATE DATABASE` 均被拒（5203） | 只读执行可以依赖 StarRocks 的权限，再叠加 Avelis 自己的语句校验 |
| 14 | 视图权限语义 | 视图是 `SECURITY NONE`：只读账号**没有** Catalog 表的 SELECT 时，仍能查询引用这些表的视图 | 授予一个视图等于暴露它引用的所有来源数据。Avelis 的权限检查要沿血缘看到视图的来源，不能只看视图本身 |
| 15 | Catalog 内表的授权 | 在 Catalog 内执行：`SET CATALOG shop_mysql; GRANT SELECT ON ALL TABLES IN ALL DATABASES TO USER …` | 已写入 `demo.py seed` |
| 16 | 超时 | `SET query_timeout = 2` 后 `SELECT sleep(5)` 在 2.01 秒被中止，错误 5024：“Query reached its timeout of 2 seconds” | 查询超时可由会话变量保证；错误码 5024 映射为“查询超时”，如实返回 |
| 17 | 内存与行数限制 | 默认 `query_mem_limit = 0`（不限）、`sql_select_limit` 极大 | 执行器必须在会话里显式设置内存上限，并由编译出的 SQL 自带 `LIMIT` |
| 18 | MySQL 侧身份与键 | 采集账号可读 `@@server_uuid`、`@@hostname`、`@@port`；`information_schema.statistics` 给出主键与唯一键（如 `dim_region` 的 `uk_region_name`） | 同一个库在 StarRocks 里是 `shop-mysql:3306`，在采集器看来是 `127.0.0.1:13306`：外部表匹配必须用“端点别名”，并以 `server_uuid` 确认实例；唯一键是粒度候选 |
| 19 | root 口令与重启 | all-in-one 镜像每次启动都用 root 注册 BE；root 改过口令后若不传 `MYSQL_PWD`，容器会自动关停 | 演示环境在 BE 存活后才设口令，重启时由包装脚本传入 `MYSQL_PWD`（已验证重启后正常） |
| 20 | 字符集 | MySQL 初始化脚本若不声明 `SET NAMES utf8mb4`，中文会被重复编码存入；Catalog 原样读出乱码 | 演示数据已修正。采集时如发现疑似重复编码，只提示，不改写 |

## 关键证据摘录

视图定义（`information_schema.views.VIEW_DEFINITION`）：

```sql
SELECT `shop`.`orders`.`order_date`, `shop`.`orders`.`region`, sum(`shop`.`orders`.`paid_amount`) AS `sales_amount`
FROM `shop`.`orders`
GROUP BY `shop`.`orders`.`order_date`, `shop`.`orders`.`region`
```

带别名、引用 Catalog 表的视图（`SHOW CREATE VIEW ads.sales_by_category` 节选）：

```sql
SELECT `shop`.`o`.`order_date`, `shop_mysql`.`shop_dim`.`c`.`category_name`, sum(`shop`.`i`.`item_amount`) AS `item_sales`
FROM `shop`.`order_items` AS `i` INNER JOIN `shop`.`orders` AS `o` ON `shop`.`i`.`order_id` = `shop`.`o`.`order_id`
INNER JOIN `shop_mysql`.`shop_dim`.`dim_product` AS `p` ON ...
```

旧式外部表（`SHOW CREATE TABLE dim.ext_region` 节选，口令被 StarRocks 置空）：

```sql
) ENGINE=MYSQL
PROPERTIES ("host" = "shop-mysql", "port" = "3306", "user" = "sr_legacy", "password" = "",
            "database" = "shop_dim", "table" = "dim_region");
```

## 对实现的直接影响

1. 定义采集用 `information_schema.views`；解析前做别名归一化（第 2 条）。
2. 资产身份：内表用 `TABLE_ID`；视图与外部表用“库 + 名称 + 创建时间 + 定义哈希”，并支持可选的 OPERATE 增强（第 3、4 条）。
3. 粒度：主键表直接观察；明细表、视图、外部表走声明、解析与核验（第 6 条）。
4. 变更检测必须覆盖“定义没变但上游列没了”（第 9 条）。
5. 外部表：两种写法都要采集；端点别名 + `server_uuid` 确认（第 10、12、18 条）。
6. 权限：视图沿血缘继承来源的敏感度（第 14 条）。
7. 执行器：会话设置 `query_timeout`、`query_mem_limit`，SQL 自带 `LIMIT`；5024 映射为超时（第 16、17 条）。

## DDL 变更归因（AuditLoader）

2026-11-01 已在演示环境的 StarRocks 3.5.14 + AuditLoader 4.2.2 上实机验证 DDL 审计归因。StarRocks 声明可选的 `DDL_AUDIT` 能力：安装并配置官方 AuditLoader 插件、授予采集账号审计表的 `SELECT` 权限后，在数据源选项 `ddl_audit_table` 中填写 `库.表`，名称可用反引号引用。官方示例表名 `starrocks_audit_db__.starrocks_audit_tbl__` 只出现在配置提示中，Avelis 不自动绑定或创建该表；新建数据源时可以留空，留空按 `unsupported` 处理并明确提示“未绑定审计表”，不发起审计读取。

以下建表示例对应 **AuditLoader 4.2.2** 的 Stream Load 列清单，27 列的名称与顺序均须一致；该版本没有 `warehouse` 列。其他插件版本须按其实际列清单核对，不能直接沿用此表。

```sql
CREATE DATABASE IF NOT EXISTS starrocks_audit_db__;
CREATE TABLE starrocks_audit_db__.starrocks_audit_tbl__ (
    `queryId` VARCHAR(48),
    `timestamp` DATETIME,
    `queryType` VARCHAR(12),
    `clientIp` VARCHAR(128),
    `user` VARCHAR(128),
    `authorizedUser` VARCHAR(128),
    `resourceGroup` VARCHAR(128),
    `catalog` VARCHAR(128),
    `db` VARCHAR(128),
    `state` VARCHAR(128),
    `errorCode` VARCHAR(128),
    `queryTime` BIGINT,
    `scanBytes` BIGINT,
    `scanRows` BIGINT,
    `returnRows` BIGINT,
    `cpuCostNs` BIGINT,
    `memCostBytes` BIGINT,
    `stmtId` INT,
    `isQuery` TINYINT,
    `feIp` VARCHAR(128),
    `stmt` VARCHAR(1048576),
    `digest` VARCHAR(32),
    `planCpuCosts` DOUBLE,
    `planMemCosts` DOUBLE,
    `pendingTimeMs` BIGINT,
    `candidateMVs` VARCHAR(65533),
    `hitMvs` VARCHAR(65533)
)
ENGINE = OLAP
DUPLICATE KEY(`queryId`, `timestamp`, `queryType`)
DISTRIBUTED BY HASH(`queryId`) BUCKETS 1
PROPERTIES ("replication_num" = "1");
```

此示例使用演示环境的单副本；生产环境的副本数、分桶与保留策略须按集群配置确定。插件写入账号与 Avelis 采集账号分别授权，后者只需读取该表。

版本与字段按实际来源检查，不凭 StarRocks 版本号猜表结构。[StarRocks 3.3 的 AuditLoader 文档](https://docs.starrocks.io/docs/3.3/administration/management/audit_loader/) 给出的 5.0.0 插件示例支持 StarRocks 3.3.11+；[插件仓库的 4.2.1 示例](https://github.com/StarRocks/fe-plugins-auditloader/blob/b98cfa246bb8c437ecf14caf3ff576810084003f/README.md) 写的是 StarRocks 2.5+。这两份示例不构成完整的历史兼容矩阵。适配器要求 `timestamp`、`user`、`db`、`stmt`、`state`，以及 `isQuery`、`queryType`、`stmtType`、`stmt_type` 中至少一列；字段名比较不区分大小写。缺列或基本字段类型不符，降级为 `incompatible` 并说明缺少或不兼容的字段。

只采用 `state` 为 `EOF` 或 `OK` 的成功记录；有 `isQuery` 时要求为 false/0。`queryType` 是审计日志类别，不是 SQL 语句类型：`query`、`slow_query` 可包含 DDL，`connection` 排除，仍须按 SQL 的 `CREATE`、`ALTER`、`DROP`、`RENAME`、`TRUNCATE` 白名单解析对象与列，排除 `SELECT` 和 DML。范围仅限 `default_catalog`，明确指向其他 Catalog 的语句跳过，外部 Catalog 的同名资产归因为 `unsupported`。执行人原样取 `user`，保留可能存在的 `@host`，不裁掉 host，也不改取 `authorizedUser`，以免合并不同身份。对象、列、执行人或时间不能可靠确定时，沿用 [ADR 0008](../decisions/0008-history-integrity.md) 的保守归因规则，保留未知或歧义。

审计表有 `catalog` 列时，按实际字段名读取（字段发现不区分大小写），一、二段对象名按该行的会话 Catalog 解释。`catalog` 非空且不是 `default_catalog` 时，这些目标排除；显式三段名以语句中的 Catalog 为准。这样 `SET CATALOG` 到外部 Catalog 后的 `DROP TABLE sales` 不会成为内表同名资产的候选。`catalog` 为空或旧版 AuditLoader 表没有此列时，保持按 `default_catalog` 解释的兼容行为；**缺少此列就无法区分外部会话中的一、二段目标，仍可能误纳入同名内表的候选**，应使用包含并正确写入 `catalog` 的审计表。

`CREATE TEMPORARY TABLE` 与 `DROP TEMPORARY TABLE` 排除，不用临时表 DDL 解释同名正式表的持久元数据变化（已在 StarRocks 3.5.14 实机验证）。列名依 StarRocks 规则不区分大小写，包括反引号引用的列名；不同用户对 `channel` 与 `` `CHANNEL` `` 的操作都保留为竞争候选。

外挂插件使用 FE JVM 默认时区把时间写为无时区的 `timestamp`（见[固定版本源码](https://github.com/StarRocks/fe-plugins-auditloader/blob/b98cfa246bb8c437ecf14caf3ff576810084003f/src/main/java/com/starrocks/plugin/audit/AuditLoaderPlugin.java#L61)）。读取时先取 `@@system_time_zone`，仅在独立的审计连接设置 `time_zone`，再通过 `UNIX_TIMESTAMP(timestamp)` 和 `FROM_UNIXTIME` 按相同设置换算 UTC 时间与查询边界；不沿用业务查询会话的时区。StarRocks 的会话与系统时区含义见[官方时区文档](https://docs.starrocks.io/docs/administration/management/timezone/)。本次实机验证的 FE 时区为 UTC，非 UTC 时区换算未实机验证。使用前须确保所有 FE 的 JVM 默认时区一致；一次连接的探测不能证明其他 FE 的配置也一致。

AuditLoader 批量异步导入审计记录，近期变更可能尚未入表。实机观察到插件刚安装后的几十秒内执行的语句可能没有写入审计表，此时归因为未匹配（`unmatched`）。所有事件设 `may_lag=True`，现有归因规则允许把上次快照前 60 秒内的相关语句纳入边界候选；60 秒是匹配窗口容差，不承诺审计记录在 60 秒内落盘，也不会追写已完成的历史归因。读取成功不能证明审计覆盖完整。

插件会按字节[截断超长 `stmt`](https://github.com/StarRocks/fe-plugins-auditloader/blob/b98cfa246bb8c437ecf14caf3ff576810084003f/src/main/java/com/starrocks/plugin/audit/AuditLoaderPlugin.java#L251)，[默认 `max_stmt_length` 为 1048576](https://github.com/StarRocks/fe-plugins-auditloader/blob/b98cfa246bb8c437ecf14caf3ff576810084003f/src/main/java/com/starrocks/plugin/audit/AuditLoaderPlugin.java#L348)。DDL 的语句字节数达到 `stmt` 的 VARCHAR 长度与该默认上限中较小的一项、词法或括号未闭合、括号不匹配或以 `...` 结束时，按 `incompatible` 处理，不使用可能被截断的语句确定执行人。前置 SQL 注释不会隐藏 DDL；共享解析器无法可靠处理的转义引用对象名也降为 `incompatible`，不把名称前缀当成另一个对象，也不丢弃改名旧对象上的竞争用户。若管理员把插件 `max_stmt_length` 调得更短，且截断后的文本恰好仍是一条完整 SQL，单凭记录无法可靠识别；应保留足够的插件上限与表列长度。

审计来源读取失败时分别给出原因：绑定表不存在时为 `unavailable / not_enabled`，提示安装 AuditLoader 并绑定审计表；采集账号没有读取权限时为 `unavailable / no_permission`，提示授予 `SELECT`；必需字段缺失、来源结构不符或记录无法可靠解释时为 `unavailable / incompatible`；其他读取失败为 `unavailable / error`，附错误摘要。这些状态说明归因不可用，不把读取失败或审计缺失当成“没有人执行过 DDL”。
