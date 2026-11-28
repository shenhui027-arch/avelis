# TiDB 实机核验

2026-11-28 在 TiDB v8.5.4 上核验，镜像为 `pingcap/tidb:v8.5.4`，单机 unistore 存储。联调环境见 `deploy/demo/tidb/compose.yaml`，这是独立的 Compose 项目 `avelis-tidb`；联调测试见 `backend/tests/metadb/test_tidb_collect.py`。下面的结论只适用于这个版本和部署方式，不推及 TiKV 集群或其他版本。

## 接入方式

- TiDB 兼容 MySQL 协议与 SQL，适配器继承 MySQL 连接器，方言借用 SQLGlot 的 `mysql`（ADR 0004，2026-11-28 范围扩展）。
- 资产名与 MySQL 相同，为 `def.库.表`，生成的查询只写 `库.表`。
- 声明的能力：采集元数据、视图血缘、问数查询、执行前核对。默认端口 4000。

## 逐项核验

| 项目 | 实测结果 | 适配器的处理 |
|---|---|---|
| 表、列、注释、主键与唯一键 | `information_schema.tables / columns / statistics` 与 MySQL 一致 | 沿用 MySQL 的采集查询 |
| 视图定义 | `information_schema.views.VIEW_DEFINITION` 是补全了库名、带反引号的规范文本，如 `` SELECT `o`.`user_id` AS `user_id`,SUM(`o`.`amt`) … FROM `shop`.`orders` AS `o` `` | 交 `mysql` 方言解析，列级血缘、过滤、关联键与分组都能识别 |
| 实例身份 | `@@server_uuid` 恒为全零；unistore 下没有可读的集群 ID | `instance_id` 留空，不做实例变化检测；只记录主机名、端口与版本 |
| 物理身份 | `information_schema.tables.TIDB_TABLE_ID` 在 `TRUNCATE` 后会换新值，`ALTER TABLE` 不变 | 不作物理身份，表与视图按名称对应；否则清空数据会被当成删表重建 |
| 只读事务 | `START TRANSACTION READ ONLY` 与 `transaction_read_only` 只有空实现，报错 1235 | 改用历史读事务 `START TRANSACTION READ ONLY AS OF TIMESTAMP NOW(3) - INTERVAL 1 SECOND`，写入由引擎拒绝（root 账号实测同样被拒） |
| 超时 | `MAX_EXECUTION_TIME` 生效，超时报 3024 | 归为 `timeout`。`SLEEP()` 被打断时返回 1 而不报错，联调改用三方笛卡尔积验证 |
| 行数上限 | `SQL_SELECT_LIMIT` 生效 | 沿用 MySQL |
| 内存上限 | `tidb_mem_quota_query` 按查询生效，超出报 8175 | `mem_limit_bytes` 映射到它；8175 归为 `other`，原文保留 |
| DDL 记录 | `information_schema.ddl_jobs` 有语句、对象与时间，但没有执行用户；`statements_summary` 只按语句指纹保留一个样例用户，且只存在内存里 | 不声明 DDL 归因，归因状态为 `unsupported`，原因写明“DDL 作业记录不含执行用户” |

## 已知局限

- 问数读取约 1 秒前的数据（历史读）。刚建不到 1 秒的表在历史读里还不存在，会报错。
- 历史读事务里读取 `information_schema` 内存表会报 8024（invalid transaction）。问数只查业务表，不受影响。
- 没有实例变化检测：数据源地址被改到另一个 TiDB 集群时，采集不会提示“实例变化”。
- TiKV 集群部署、TiFlash 副本与分区表的元数据没有联调。
