# M7 验收说明：更多引擎

- 日期：2026-10-29
- 设计：[ADR 0004](../../decisions/0004-engine-adapters.md)（引擎适配层，已接入引擎的实测要点）
- 完成标准：每种新引擎都在真实实例上走同一组场景，不使用模拟结果。
- 引擎与演示实例（`scripts/demo.py up --engines` 启动，compose 的 `engines` 配置）：

  | 引擎 | 镜像 | 说明 |
  |---|---|---|
  | ClickHouse 26.8 LTS | `clickhouse/clickhouse-server` | HTTP 接口 |
  | SQL Server 2022 Developer | `mcr.microsoft.com/mssql/server` | 用户已确认接受许可条款（2026-10-29） |
  | Oracle Database 23ai Free | `gvenzl/oracle-free`（slim-faststart） | 用户已确认接受许可条款（2026-10-29）；服务名 FREEPDB1 |
  | Apache Doris 4.1 | `apache/doris` all-in-one | 一个 FE + 一个 BE；不挂数据卷，启动后由 demo.py 设置 root 口令并灌数 |

- 测试：
  - 通用场景 [backend/tests/acceptance/engine_suite.py](../../../backend/tests/acceptance/engine_suite.py)，每种引擎 7 项，共 28 项。各引擎只在 `test_m10_engine_*.py` 中给出差异：连接配置、DDL 写法、标识符大小写、错误归类。
  - 单元测试 [backend/tests/unit/test_engines.py](../../../backend/tests/unit/test_engines.py)：方言差异（Catalog 段、`TOP` / `FETCH FIRST`、`N'...'`、`DATE` 区间、标识符折叠）、类型写法、身份规则。
  - 一条命令验收默认包含这些引擎；`uv run scripts/acceptance.py --no-engines` 可以跳过它们。

## 演示数据

四种引擎各有一个与 PostgreSQL 门店库相同的 `store` 库：shops、sales、returns 三张表，以及视图 city_daily_sales。

| 项 | 值 |
|---|---|
| 2026-09-15 上海 | 250.00（作废单不计） |
| 2026-09-15 北京 | 80.00（Oracle 中其下单时间为 10:30） |
| returns.sale_id = 9002 | 两条（用于粒度核验） |
| sales.amount | 有列注释“成交金额”；其他列多数没有注释 |

**账号**：每种引擎两个，口令由 `init-env` 生成，满足 SQL Server、Oracle 的复杂度要求。
- 采集账号只能读元数据；
- 查询账号只能读数据（ClickHouse 为 `readonly=2`；SQL Server、Doris 另有读视图定义的元数据权限，供执行前核对使用）。

## 场景与实际行为（每种引擎）

| # | 场景 | 断言的实际行为 |
|---|---|---|
| 00 | 建数据源并采集 | 缺少必填配置时 422，并给出配置名；采集到 4 个资产，视图类型正确 |
| 01 | 视图血缘、粒度、注释 | 视图解析状态为 ok，字段推导指纹全部正常，视图粒度为（日期，城市）；sales 观察到的粒度：有唯一键的引擎为 sale_id，ClickHouse 为空（排序键不保证唯一）；`amount` 注释状态为 ok，`status` 为 missing；列级关系为“聚合、过滤、关联键” |
| 02 | 发布口径并问数 | 上海 250.00、北京 80.00；SQL 符合该引擎的写法（见下表）；“各城市前 1”为上海 |
| 03 | 粒度核验 | 在 returns 上错误声明 sale_id 为粒度，核验结果为不成立，样例 9002；SQL 中的表名按该引擎加引用符 |
| 04 | 改名与删后重建 | 改名后仍是同一资产，并有“改名”事件；删后重建后是新资产，旧资产标为已删除 |
| 05 | 改视图定义（去掉作废单过滤） | 采集前问数，被执行前核对拦截（`metadata_stale`，没有返回结果）；采集后出现定义变化与推导变化事件，口径受影响，问数被拦截且不生成 SQL |
| 06 | 超时、错误、只读 | 超时、列不存在、写入分别归为超时、对象无法解析、权限；数据未被改动；采集账号读业务数据被拒绝（Oracle 报 ORA-00942，按“对象无法解析”归类） |

**各引擎生成的问数 SQL**

| 引擎 | FROM | 行数上限 | 其他 |
|---|---|---|---|
| ClickHouse | `store.city_daily_sales AS b`（不写 Catalog 段） | `LIMIT` | |
| SQL Server | `storedb.store.city_daily_sales AS b` | `TOP` | 字符串写成 `N'上海'` |
| Oracle | `STORE.CITY_DAILY_SALES b`（不写服务名） | `FETCH FIRST n ROWS ONLY` | `SALE_DATE >= '… 00:00:00' AND < 次日 0 点` |
| Doris | `internal.store.city_daily_sales AS b` | `LIMIT` | |

## 实测中发现并处理的差异

- **ClickHouse**：
  - 普通视图不支持 `MODIFY QUERY`，`CREATE OR REPLACE VIEW` 会换新 UUID。因此视图不用 UUID 作身份，按名称对应。
  - 查询账号读 `system.parts` 需要单独授权：只授予 4 列；未授权时不报告新鲜度。
- **SQL Server**：
  - 查询账号没有 `VIEW DEFINITION` 时，读到的视图定义为空，执行前核对会误拦。已在授权中补上，并写入引擎说明。
  - 驱动会把服务器报错与附加说明拼在一起，例如 208 后面跟着 20018。现在保留服务器原文与错误号，只有驱动层错误（20003 超时）才取驱动的编号。
- **Oracle**：
  - 视图原文的小写表名对不上数据字典中的大写名，新增了按 Oracle 规则的标识符折叠；
  - 23ai 的 ORA-41900（缺少某项权限）归为权限；
  - `NUMBER` 取为 `Decimal`。
- **Doris**：`information_schema.tables` 没有表 ID，改用 `metadata_name_ids.TABLE_ID`。

## 尚未验证

- 各引擎的外部 Catalog / 外部表追溯（Doris 的 JDBC Catalog、ClickHouse 的 MySQL 表引擎、SQL Server 链接服务器、Oracle 数据库链接与同义词）。
- 物化视图：ClickHouse 的 `TO` 目标表、Oracle 物化视图、Doris 异步物化视图。
- 带引号的大小写混合标识符（Oracle、SQL Server）。
- 多节点部署、生产权限体系与更大的数据量。
- Hive：尚未接入。
