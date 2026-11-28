# Trino 实机核验

2026-11-28 在 Trino 482 上核验，镜像为 `trinodb/trino:482`，单节点。
- 联调环境见 `deploy/demo/trino/compose.yaml`（独立 Compose 项目 `avelis-trino`），配了两个 Catalog：`memory` 是内存连接器，`pg` 是 PostgreSQL 连接器。
- 联调测试见 `backend/tests/metadb/test_trino_collect.py`。
- 驱动为 `trino`（trino-python-client 0.340）。

下面的结论只适用于这个版本和这两种连接器，不推及 Hive / Iceberg 等其他连接器或多节点集群。

## 接入方式

- 一个数据源对应一个 Catalog（选项 `catalog`），采集其中配置的模式（选项 `schemas`）。
- 资产名为 `Catalog.模式.表`，与 PostgreSQL 的“库.模式.表”同形；生成的 SQL 写全三段。
- 声明的能力：采集元数据、视图血缘、问数查询、执行前核对。默认端口 8080。
- 认证：`NONE` 只传用户名，适用于服务端未开启认证；`PASSWORD` 走 HTTP Basic，Trino 要求必须用 HTTPS。

## 逐项核验

| 项目 | 实测结果 | 适配器的处理 |
|---|---|---|
| 表与视图 | `<catalog>.information_schema.tables` 的 `table_type` 为 `BASE TABLE` / `VIEW` | 按 Catalog 与配置的模式采集；配置的模式在 `information_schema.schemata` 中不可见时整次采集失败 |
| 列与列注释 | `information_schema.columns` 没有注释列；`system.jdbc.columns.remarks` 是列注释 | 列、类型、可空与注释取自 `system.jdbc.columns`，类型保留 Trino 原样（如 `decimal(18,2)`） |
| 表注释 | `system.metadata.table_comments` | 取作资产注释 |
| 视图定义 | `information_schema.views.view_definition` 是规范化后的查询文本，表名保持建视图时的写法：同 Catalog 的表可以不写 Catalog 与模式 | 按 `trino` 方言解析，默认 Catalog 与模式取视图所在位置 |
| 跨 Catalog 引用 | 视图可以引用其他 Catalog 的表（如 `pg.public.orders`） | 不在本数据源采集范围内：解析为 `partial` 并写明“未采集到的表”，已能确定的血缘边照常生成，来源记为未采集 |
| 键与物理身份 | 不暴露主键、唯一约束与表 ID | 不报告键；表与视图按名称对应 |
| 只读 | 没有只读会话，任何能连的用户都能 `INSERT` | 只读依赖 Avelis 的单条 SELECT 校验，以及访问控制中只有 SELECT 的查询账号 |
| 超时 | 会话属性 `query_max_run_time`（含排队时间）由服务端强制，超时报 `EXCEEDED_TIME_LIMIT` | 映射到截止时间；客户端请求超时多留 15 秒作兜底；归为 `timeout` |
| 内存上限 | 会话属性 `query_max_memory` 生效，超出报 `EXCEEDED_GLOBAL_MEMORY_LIMIT` | `mem_limit_bytes` 映射到它；归为 `other` |
| 行数上限 | 没有服务端行数限制 | `fetchmany(max_rows + 1)`，取够后取消剩余查询 |
| 业务时区 | 连接参数 `timezone`（`X-Trino-Time-Zone`）生效，`current_timezone()` 返回所设时区 | 按业务时区设置 |
| 错误 | `error_name` 稳定：`TABLE_NOT_FOUND`、`COLUMN_NOT_FOUND`、`PERMISSION_DENIED` 等 | 按 `error_name` 归类，`db_errno` 记 `error_name` |
| 日期加减 | `INTERVAL` 没有 `WEEK`、`QUARTER` 单位：`INTERVAL '1' QUARTER` 报 `TYPE_NOT_FOUND` | 探索查询改写为 7 天、3 个月的倍数；SQLGlot 的 Trino 生成同样修正为 `(n * INTERVAL '3' MONTH)`（fork 分支修复） |

## 已知局限

- 引擎层没有会话只读：问数账号必须在 Trino 访问控制中只有 SELECT。
- 一个数据源只采集一个 Catalog：跨 Catalog 的来源需要为那个 Catalog 另建数据源；两边的资产目前不会自动关联。
- 物化视图（`system.metadata.materialized_views`）需要 Iceberg 等连接器，联调环境没有，按普通表采集。
- 不报告数据新鲜度：Trino 没有跨连接器一致的数据更新时间。
- 只联调了 `memory` 与 `postgresql` 连接器和单节点；Hive、Iceberg 连接器与多节点集群没有联调。
