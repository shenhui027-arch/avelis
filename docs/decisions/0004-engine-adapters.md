# ADR 0004：引擎适配层——多数据库血缘的扩展方式

- 状态：已采纳
- 日期：2026-10-29

## 背景

Avelis 首期面向 StarRocks。用户明确后续要适配多种数据库的血缘，并要求后端控制耦合度，借鉴成熟的数据治理与血缘产品。范围限定为 **SQLGlot 支持的引擎**（用户决定，2026-10-29）：Oracle、PostgreSQL、Hive、Doris、MySQL、SQL Server、ClickHouse。DB2 因 SQLGlot 没有对应方言而不在范围内。

M4 开始前检查了代码，引擎差异散在核心模块里，共约 10 处：

- 解析器、指标片段、编译器、导入各自写死 `DIALECT = "starrocks"`；
- 采集里 `if ds.kind != "starrocks"` 时直接把视图标为不支持；
- 问数的维度取值、粒度核验手写反引号 SQL；
- 连接器构造按 `kind` 分支；
- 数据源接口用 `Literal["starrocks", "mysql", "fake"]` 枚举。

照这样，每加一种数据库都要改采集、解析、问数、治理四处。

## 借鉴

| 产品 | 做法 | 我们采用的部分 |
|---|---|---|
| DataHub | 每个平台是一个 ingestion source 插件，按名字注册；SQL 解析用 SQLGlot，由平台名映射到方言；数据集由“平台 + 平台实例 + 名称”标识 | 注册表；平台 → 方言；资产名包含实例与层级 |
| OpenMetadata | 每种服务类型有独立连接器和连接配置定义；元数据、血缘、使用量分成不同的采集流程 | 连接器与能力声明放在适配器里；血缘来源（视图解析、导入、人工）分开记录 |
| OpenLineage / Marquez | 数据集 = 命名空间（引擎 + 地址）+ 层级名；用可扩展的 facet 附加信息 | 资产统一三段名，层级差异由适配器声明；外部表追溯用“端点别名 + 实例身份” |
| Apache Atlas | 每种来源一个 hook / bridge，类型系统统一 | 核心只认统一的资产、列、边模型 |

## 决定

1. **新增 `avelis.engines`**：每种数据库是一个 `Engine` 适配器，在注册表登记。适配器包含：
   - 连接器工厂：采集用、只读查询用；
   - `SqlDialect`：SQLGlot 方言名、默认 Catalog、该引擎特有写法的规整函数（如 StarRocks 把别名列写成 `db.alias.col`），
     以及生成的 SQL 是否写 Catalog 段（`catalog_in_sql`；ClickHouse 等两级命名的引擎为否）；
   - 资产名层级（`name_levels`）；
   - 能力声明：`metadata`、`view_lineage`、`query`、`external_tables`、`identity_target`；
   - 默认端口、说明。
2. **核心模块不按 `kind` 分支。**
   - `lineage/sql.py` 只认 `SqlDialect`，不知道具体数据库。
   - 采集、导入、指标片段、编译与执行前校验的方言都来自数据源的引擎。
   - 维度取值、粒度核验的 SQL 用 SQLGlot 按方言生成，不再手写引用符。
   - 分层方向：解析核心 ← 引擎适配器 ← 采集 / 问数 / 导入 / 治理。`connectors` 只保留实现，构造在 `engines`。
3. **能力只声明真实联调过的。**
   - 缺少能力时，核心模块如实报“不支持”：
     - 视图解析记为 `unsupported`，原因写明；
     - 执行类操作返回 409 `CapabilityMissing`。
   - 不降级猜测。
   - MySQL 当前只有 `metadata` 和 `identity_target`；它的视图解析和问数执行没有在真实环境验证，所以不声明。
4. **统一的资产名**：仍是 `catalog.db.name` 三段。
   - 只有 库.表 两级的引擎（MySQL、Hive、ClickHouse）用适配器给的固定 Catalog 补齐。
     这些引擎的查询不认 Catalog 段：生成 SQL 时省略，执行前校验再按方言的固定 Catalog 补回后比对（M7）。
   - 有 库.模式.表 三级的引擎（PostgreSQL、SQL Server、Oracle）把库（或服务名）放在 catalog 段。
   - 同一个库在不同网络位置的地址，仍由端点别名与实例身份（如 `server_uuid`）对齐。
5. **前端按能力渲染。**
   - 新增 `GET /api/engines`；数据源详情带 `engine` 字段。
   - “添加数据源”的类型、默认端口、是否显示 Catalog 与只读查询账号，都来自后端登记的引擎与能力。
6. **新增一种数据库的步骤**：
   1. 写连接器：采集快照、实例身份；如支持问数，还要写只读执行与错误归类。
   2. 在 `engines/builtin.py` 登记 `Engine`。
   3. 在真实实例上补验收：采集、视图解析的样例集、执行与超时、错误码归类。
   4. 联调通过后再声明对应能力。

   核心模块不改。

## 已接入的引擎

| 引擎 | 版本（演示实例） | 能力 | 要点 |
|---|---|---|---|
| StarRocks | 3.5.14 | 元数据、视图血缘、问数、外部表、执行前核对 | M0–M4，见 [starrocks-notes.md](../starrocks-notes.md) |
| MySQL | 8.4 | 元数据、作为外部表的追溯目标 | 视图解析与问数未联调 |
| PostgreSQL | 18 | 元数据、视图血缘、问数、执行前核对 | M5；`pg_class.oid` 作物理身份 |
| ClickHouse | 26.8 LTS | 元数据、视图血缘、问数、执行前核对 | M7，见下 |

**ClickHouse**（M7，真实实例联调）：

- 采集账号只需 `SHOW TABLES, SHOW COLUMNS`，读 `system.tables` / `system.columns`，读不到业务数据。
- 视图定义取 `system.tables.as_select`。ClickHouse 会改写定义（如 `COUNT(DISTINCT x)` 存为 `COUNTDistinct(x)`），SQLGlot 的 `clickhouse` 方言可以解析出相同的列级血缘。
- 物理身份：表用 `system.tables.uuid`（Atomic 库中改名不变，删后重建会变）。普通视图不支持 `MODIFY QUERY`，改定义只能 `CREATE OR REPLACE`，会换新 UUID，所以视图不用 UUID，按名称对应。
- **排序键 / 主键不保证唯一**：MergeTree 只按它排序，不去重。不作为观察到的粒度，粒度需要人工声明并核验。
- 查询账号为 `readonly=2`（只读，可以调整查询设置），每次查询设 `max_execution_time` 与 `max_memory_usage`。错误码归类：159 为超时；60、47、81 为对象无法解析；497、164 为权限。
- 数据新鲜度：取 `system.parts` 中活动分片的最近修改时间。查询账号只授予这张表的 4 列；未授权时不报告新鲜度，不让问数失败。

## 规划中的引擎（未登记，未验证）

只考虑 SQLGlot 有方言的引擎；新引擎的前提是 SQLGlot 支持它的方言。

| 引擎 | SQLGlot 方言 | 资产名映射 | 需要实测的要点 |
|---|---|---|---|
| Doris | `doris` | catalog.db.table | 视图定义的存储写法是否与 StarRocks 相同（能否复用别名规整）；外部 Catalog 的连接信息 |
| Hive | `hive` | 固定 Catalog + db.table | 元数据来自 HMS 或 `SHOW CREATE TABLE`；血缘主要来自 ETL 脚本导入，而不是视图 |
| PostgreSQL | `postgres` | db.schema.table | `pg_views` / `information_schema.views` 的定义是否已全限定；`search_path` 对解析的影响 |
| Oracle | `oracle` | 服务名.schema.table | `ALL_VIEWS.TEXT` 读取；大小写与带引号标识符；同义词 |
| SQL Server | `tsql` | db.schema.table | `sys.sql_modules`；跨库三段名、链接服务器四段名 |

## 后果

- 新引擎只需新增适配器与验收，核心模块不改。
- 适配器的能力声明会出现在接口和页面上，用户能直接看到某种数据库支持到什么程度。
- 代价：
  - 解析、编译、校验的函数都多了一个 `dialect` 参数；
  - `parse_query`、`canonical_hash_of` 的签名变了；单元测试已同步。
- 行为不变：StarRocks 的规范化 SQL 与指纹算法没有变，`PARSER_VERSION` 不变，已有指纹不会因为这次重构失效。M1、M2 的验收全部重跑通过。
