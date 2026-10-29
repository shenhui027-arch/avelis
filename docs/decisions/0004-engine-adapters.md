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
| SQL Server | 2022 Developer | 元数据、视图血缘、问数、执行前核对 | M7，见下 |
| Oracle | 23ai Free | 元数据、视图血缘、问数、执行前核对 | M7，见下 |
| Doris | 4.1 | 元数据、视图血缘、问数、执行前核对 | M7，见下；外部 Catalog 未联调 |

**ClickHouse**（M7，真实实例联调）：

- 采集账号只需 `SHOW TABLES, SHOW COLUMNS`，读 `system.tables` / `system.columns`，读不到业务数据。
- 视图定义取 `system.tables.as_select`。ClickHouse 会改写定义（如 `COUNT(DISTINCT x)` 存为 `COUNTDistinct(x)`），SQLGlot 的 `clickhouse` 方言可以解析出相同的列级血缘。
- 物理身份：表用 `system.tables.uuid`（Atomic 库中改名不变，删后重建会变）。普通视图不支持 `MODIFY QUERY`，改定义只能 `CREATE OR REPLACE`，会换新 UUID，所以视图不用 UUID，按名称对应。
- **排序键 / 主键不保证唯一**：MergeTree 只按它排序，不去重。不作为观察到的粒度，粒度需要人工声明并核验。
- 查询账号为 `readonly=2`（只读，可以调整查询设置），每次查询设 `max_execution_time` 与 `max_memory_usage`。错误码归类：159 为超时；60、47、81 为对象无法解析；497、164 为权限。
- 数据新鲜度：取 `system.parts` 中活动分片的最近修改时间。查询账号只授予这张表的 4 列；未授权时不报告新鲜度，不让问数失败。

**SQL Server**（M7，真实实例联调；用户已确认接受 Developer Edition 的许可条款）：

- 资产名 `库.模式.表`，库放在 catalog 段（与 PostgreSQL 相同）；生成的 SQL 用三段名，行数上限写成 `TOP n`。
- 采集账号只需模式上的 `VIEW DEFINITION`：读 `sys.objects`、`sys.columns`、`sys.indexes`、`sys.sql_modules`，以及 `MS_Description` 扩展属性（列注释）。读不到业务数据。
- `sys.sql_modules` 存的是整条 `CREATE VIEW` 语句，用 SQLGlot（`tsql`）取出其中的查询再解析。
- 物理身份用 `object_id`：`sp_rename` 和 `CREATE OR ALTER VIEW` 都不改变它，删除后重建会变。
- 唯一键：主键、唯一约束、非筛选的唯一索引。
- **字符串字面量写成 `N'...'`**（方言项 `national_strings`）。`NVARCHAR` 列在默认的非 UTF-8 排序规则下，`'上海'` 会先转成代码页而无法匹配。演示库刻意使用默认排序规则来覆盖这种情况。
- 查询账号除 `SELECT` 外还需要 `VIEW DEFINITION`：执行前核对要读视图定义。读不到定义时会当作定义已变化而拦截，宁可拦截，不放过。
- 错误归类：
  - 驱动层的 20003 为超时，查询超时后连接被驱动断开；
  - 207、208、4104 为对象无法解析；
  - 229、230、262、297 为权限。

  服务器原始报错原样保留，去掉驱动附加的说明。
- 不报告数据新鲜度：最近写入时间需要 `VIEW SERVER STATE`，默认不授予。

**Oracle**（M7，真实实例联调；用户已确认接受 Oracle Free 的许可条款）：

- 资产名 `服务名.模式.表`。查询中不写服务名（`catalog_in_sql = False`），行数上限写成 `FETCH FIRST n ROWS ONLY`，执行前校验同样识别。
- **标识符大小写**：未加引号的名字在数据字典中存为大写。解析前用 `fold_identifiers("oracle")` 按 Oracle 规则折叠，视图原文的 `store.sales` 才能对上已采集的 `STORE.SALES`。这一规整只挂在 Oracle 方言上，不改变其他引擎的规范化 SQL 与指纹。
- **`DATE` 带时间部分**（方言项 `date_has_time`）：按日期过滤时，用“当日 0 点 ≤ t < 次日 0 点”的区间，而不是 `BETWEEN` 两个日期。
  - 演示库里北京那笔订单的下单时间是 10:30，用来确认这一点：用 `BETWEEN` 会漏掉它。
  - 会话固定 `NLS_DATE_FORMAT` 与 `NLS_TIMESTAMP_FORMAT`，SQL 中的日期字符串不依赖库的默认设置。
- 采集账号只需 `CREATE SESSION` 与 `SELECT_CATALOG_ROLE`，读 `DBA_*` 数据字典。
  - 读业务数据会报 ORA-00942：Oracle 对无权限的对象报“不存在”，因此按“对象无法解析”归类，不改写成“权限”。
- 物理身份用 `OBJECT_ID`：`RENAME` 与 `CREATE OR REPLACE VIEW` 都不改变它。
- 视图定义取 `DBA_VIEWS.TEXT`（建视图时的原文）。
- 问数执行：
  - 在只读事务（`SET TRANSACTION READ ONLY`）内执行，并设调用超时；
  - `NUMBER` 取为 `Decimal`，默认的 float 会让金额产生精度误差；
  - 错误归类：DPY-4024 为超时；ORA-00904、00942 为对象无法解析；ORA-01031、01456、41900 为权限（ORA-41900 是 23ai 起的“缺少某项权限”）。
- 不报告数据新鲜度：`DBA_TAB_MODIFICATIONS` 依赖统计信息刷新，不能代表最近写入时间。

**Doris**（M7，真实实例联调；all-in-one 镜像，一个 FE 加一个 BE）：

- 资产名 `internal.库.表`。视图定义的存储写法与 StarRocks 相同（`internal`.`db`.`alias`.`col`），复用 StarRocks 的别名前缀规整。
- 采集账号只需库上的 `SHOW_VIEW_PRIV`：`information_schema` 的 tables、columns、views、table_options、metadata_name_ids 全部可见，读业务数据会被拒绝。
- 物理身份用 `metadata_name_ids.TABLE_ID`：表改名不变，删后重建会变。**视图也有稳定 ID**，`ALTER VIEW`、`CREATE OR REPLACE VIEW` 都不变，这一点与 StarRocks 不同。
- 键模型取 `table_options.TABLE_MODEL`：只有 `UNI`（唯一键模型）作为数据库保证的唯一键；`DUP`、`AGG` 不作为粒度依据。
- 问数执行：会话设 `query_timeout`、`exec_mem_limit`。Doris 的错误号统一为 1105，按报错内容归类：
  - query timeout 为超时；
  - Access denied、command denied 为权限；
  - Unknown column、does not exist 为对象无法解析。
- 查询账号需 `SELECT_PRIV` 与 `SHOW_VIEW_PRIV`：执行前核对要读视图定义。
- 数据新鲜度：`information_schema.tables.UPDATE_TIME`（写入后更新）。

## 规划中的引擎（未登记，未验证）

只考虑 SQLGlot 有方言的引擎；新引擎的前提是 SQLGlot 支持它的方言。

| 引擎 | SQLGlot 方言 | 资产名映射 | 需要实测的要点 |
|---|---|---|---|
| Hive | `hive` | 固定 Catalog + db.table | 元数据来自 HMS 或 `SHOW CREATE TABLE`；血缘主要来自 ETL 脚本导入，而不是视图 |
| PostgreSQL | `postgres` | db.schema.table | `pg_views` / `information_schema.views` 的定义是否已全限定；`search_path` 对解析的影响 |

## 后果

- 新引擎只需新增适配器与验收，核心模块不改。
- 适配器的能力声明会出现在接口和页面上，用户能直接看到某种数据库支持到什么程度。
- 代价：
  - 解析、编译、校验的函数都多了一个 `dialect` 参数；
  - `parse_query`、`canonical_hash_of` 的签名变了；单元测试已同步。
- 行为不变：StarRocks 的规范化 SQL 与指纹算法没有变，`PARSER_VERSION` 不变，已有指纹不会因为这次重构失效。M1、M2 的验收全部重跑通过。
