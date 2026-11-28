# Spark SQL 实机核验

2026-11-28 在 Spark 4.0.1 上核验，镜像为 `apache/spark:4.0.1`。
- Spark Thrift Server 以 `local[2]` 运行，使用内嵌 Derby 元数据库。
- 联调环境见 `deploy/demo/spark/compose.yaml`（独立 Compose 项目 `avelis-spark`）。
- 联调测试见 `backend/tests/metadb/test_spark_collect.py`。

下面的结论只适用于这个版本和部署方式，不推及接 Hive Metastore 的集群、Delta / Iceberg 表或 Databricks。

## 接入方式

- Spark Thrift Server 说 HiveServer2 协议。适配器继承 Hive 连接器，连接、认证（NONE / NOSASL / LDAP）、TLS、异步执行与到期取消都相同，只覆盖 Spark 不同的地方。
- 方言为 SQLGlot 的 `spark`。
- 资产名两级：`库.表`，固定 Catalog 段 `spark_catalog`；生成的 SQL 不写 Catalog 段。
- 声明的能力：采集元数据、视图血缘、问数查询、执行前核对。默认端口 10000。

## 逐项核验

| 项目 | 实测结果 | 适配器的处理 |
|---|---|---|
| 列出表 | `SHOW TABLES IN 库` 有三列：namespace、tableName、isTemporary | 取表名，跳过临时视图 |
| 表结构 | `DESCRIBE FORMATTED` 是“键 / 值”两列（键后没有冒号）；普通列里也列出分区列；`Type` 为 `MANAGED` / `EXTERNAL` / `VIEW`；`Table Properties` 写成 `[k=v, …]` | 单独解析，结果与 Hive 同形：分区列排在普通列之后、只列一次，表注释取 `Comment` |
| 视图定义 | 保存原文（`View Text`），不补库名；不写库名的表按 `View Catalog and Namespace`（建视图时的当前库）解析，不一定是视图所在的库 | 定义取 `View Text`；建视图时的库记在资产属性 `view_context.db`，采集按它解析 |
| 只读 | 没有只读会话；`TRANSFORM … USING '<脚本>'` 与 `reflect` 都能执行 | 只读依赖单条 SELECT 校验与只有 SELECT 权限的账号；`TRANSFORM`、`reflect`、`java_method` 由共用禁止清单拒绝 |
| 会话参数 | 经 `execute_async` 的 configuration 传入无效 | 先执行 `SET spark.sql.session.timeZone`（业务时区）与 `SET spark.sql.thriftServer.queryTimeout`（兜底超时） |
| 超时 | 服务端超时精确生效，但报 `TIMEDOUT_STATE`（8），impyla 不认识，抛 `KeyError(8)`；状态查询每次最多阻塞约 5 秒 | 由 Avelis 到期取消，最多晚约 5 秒；服务端超时设为截止时间加 30 秒，只作兜底 |
| 错误 | Spark 4 报错带错误类，如 `[TABLE_OR_VIEW_NOT_FOUND]`、`[UNRESOLVED_COLUMN.WITH_SUGGESTION]` | 按错误类归类，`db_errno` 记错误类全名 |
| 日期加减与截断 | `INTERVAL '1' QUARTER` 报 `INVALID_TYPED_LITERAL`；`DATE_TRUNC('WEEK', …)` 按周一截断 | 探索查询把按季、按周加减改写为月、天；SQLGlot 的 Hive / Spark 生成同样修正（fork 分支修复） |
| 数据新鲜度 | `SHOW PARTITIONS` 不支持 `ORDER BY` / `LIMIT`；分区的 `Partition Parameters` 有 `transient_lastDdlTime`；非分区的数据源表（`USING parquet`）没有 | 取最大的分区说明，读它的写入时间；非分区数据源表不报告新鲜度 |

## 已知局限

- 引擎层没有会话只读：问数账号必须只有 SELECT 权限。
- 超时取消最多晚约 5 秒；没有每查询内存上限。
- 非分区的数据源表不报告数据新鲜度；分区按字符串取最大值，日期分区值需零填充。
- 接 Hive Metastore 的集群、Delta / Iceberg 表、Kerberos 认证与 http 传输没有联调。
