# StarRocks 3.5.14 实机核验记录

- 环境：本机 Docker，`starrocks/allin1-ubuntu:3.5.14`（`3.5.14-23a56ec`，单 FE + 单 BE），电商维表库 MySQL 8.4.10，见 [ADR 0001](../decisions/0001-demo-environment.md)。
- 方法：`uv run scripts/demo.py verify` 逐项真实执行（`scripts/sr_probe.py`），原始结果写入 `.demo/sr-verify.json`（不进仓库，口令已替换为 `<redacted>`）。破坏性试验只在临时库 `avelis_probe` 中进行。
- 日期：2026-10-29。
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
