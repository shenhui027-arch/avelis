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
   - MySQL 声明 `metadata`、`identity_target`、`view_lineage`、`query`、`live_check`、`ddl_audit`；其中 DDL 归因需要显式绑定可读的审计表，离线验证与真实实例验收分开说明。
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
| StarRocks | 3.5.14 | 元数据、视图血缘、问数、外部表、执行前核对、可选 DDL 审计归因（`DDL_AUDIT`） | M0–M4；审计需安装 AuditLoader、授权采集账号 SELECT，并通过 `options.ddl_audit_table` 绑定审计表；见 [StarRocks 实机核验与审计说明](../engines/starrocks.md) |
| MySQL | 8.4 | 元数据、视图血缘、问数、执行前核对、DDL 归因、作为外部表的追溯目标 | DDL 审计表可选绑定；来源与证据边界见下 |
| PostgreSQL | 18 | 元数据、视图血缘、问数、执行前核对 | M5；`pg_class.oid` 作物理身份 |
| ClickHouse | 26.8 LTS | 元数据、视图血缘、问数、执行前核对 | M7，见下 |
| SQL Server | 2022 Developer | 元数据、视图血缘、问数、执行前核对、变更归因 | M7；新增默认跟踪归因仅离线单元测试验证，见下 |
| Oracle | 23ai Free | 元数据、视图血缘、问数、执行前核对、变更归因 | M7；新增统一审计归因仅离线单元测试验证，见下 |
| Doris | 4.1 | 元数据、视图血缘、问数、执行前核对 | M7，见下；外部 Catalog 未联调 |
| Hive | 4.1.0 | 元数据、视图血缘 | 见下；不支持问数；认证 NONE / NOSASL / LDAP |

### MySQL

MySQL 8.4 适配器补齐视图血缘、问数、执行前核对与可选 DDL 归因；新增能力由离线单元测试覆盖，真实实例中的账号权限、超时和审计配置仍需部署方验收。

- 资产名为 `def.库.表`；查询 SQL 只写 `库.表`，执行前核对按固定 Catalog 补回后比较。
- 视图定义取 `information_schema.VIEWS.VIEW_DEFINITION`。MySQL 会改写定义并补充库名与反引号，直接交给 SQLGlot 的 `mysql` 方言解析列级血缘，保留采集到的定义用于执行前核对。
- 问数通过 `pymysql` 使用独立的只读账号，设置 `MAX_EXECUTION_TIME`、驱动超时与返回行数上限，并在只读事务内执行。查询账号需要 `SELECT` 与 `SHOW VIEW`，供执行前核对读取涉及对象的结构和视图定义。`information_schema.tables.UPDATE_TIME` 不能可靠代表最近写入时间，因此不报告数据新鲜度。
- DDL 审计来源通过数据源选项 `ddl_audit_table` 显式绑定，填写 `库.表`，提示值为 `mysql.general_log`；未绑定时没有审计来源，不会自动读取该表。绑定通用查询日志表时，要求 `general_log=ON`、`log_output` 包含 `TABLE` 且不含 `NONE`，采集账号有该表的 `SELECT` 权限（`NONE` 优先于 `TABLE`）。审计插件表需提供兼容的 `event_time`、`user_host`、`command_type`、`argument` 字段；`event_time` 只接受 `TIMESTAMP`（可带精度），不接受没有时区契约的 `DATETIME`，否则报告 `incompatible`。
- 审计查询粗筛 `CREATE`、`ALTER`、`DROP`、`RENAME`、`TRUNCATE` 请求，再解析涉及的对象、列以及 `user_host` 中的用户名。候选读取上限为 10000 条；超限时报告 `unavailable` / `error`，明确说明无法完整归因，不截断后继续给出 `matched`。
- 通用查询日志记录的是服务器**收到**的语句，既不证明执行成功，也不保证与执行顺序一致；记录只作为候选证据，与采集到的结构变化、时间窗口、对象名及可解析的列名、重定义等语法证据共同判断。日志没有可靠的当前库上下文；遇到未带库名的目标、含可能影响采集范围 DDL 的真正多语句请求，或 MySQL 可执行注释时，报告 `unavailable` / `incompatible`，明确提示无法可靠确定候选，避免遗漏候选后错误归因。显式 `TEMPORARY` 对象排除；`CREATE INDEX` / `DROP INDEX` 以 `ON` 后的表作为候选，但保守记为 `ambiguous`。其他能确认目标但无法解析动作的请求也保留候选，保守记为 `ambiguous`。归因状态沿用 ADR 0008。[MySQL 官方文档：通用查询日志](https://dev.mysql.com/doc/refman/8.4/en/query-log.html)
- `mysql.general_log.event_time` 为 `TIMESTAMP`，存储时转换为 UTC，读取时按会话 `time_zone` 转换。审计读取会话设为 `+00:00`，统一以 UTC 与结构变更时间比较；`log_timestamps` 只影响文件日志的时间戳，不影响日志表。[MySQL 官方文档：日期时间类型](https://dev.mysql.com/doc/refman/8.4/en/datetime.html)、[通用查询日志时间戳](https://dev.mysql.com/doc/refman/8.4/en/query-log.html)
- 审计来源不可用时如实区分：未绑定审计表；`not_enabled`（`general_log=OFF`、`log_output` 不含 `TABLE` 或包含 `NONE`）；`no_permission`（采集账号不能读取审计表）；`incompatible`（来源结构不符合要求，或无法可靠确定候选）；`error`（其他读取失败，保留错误摘要）。这些情况不猜测变更人。

**ClickHouse**（M7，真实实例联调）：

- 采集账号只需 `SHOW TABLES, SHOW COLUMNS`，读 `system.tables` / `system.columns`，读不到业务数据。
- 视图定义取 `system.tables.as_select`。ClickHouse 会改写定义（如 `COUNT(DISTINCT x)` 存为 `COUNTDistinct(x)`），SQLGlot 的 `clickhouse` 方言可以解析出相同的列级血缘。
- 物理身份：表用 `system.tables.uuid`（Atomic 库中改名不变，删后重建会变）。普通视图不支持 `MODIFY QUERY`，改定义只能 `CREATE OR REPLACE`，会换新 UUID，所以视图不用 UUID，按名称对应。
- **排序键 / 主键不保证唯一**：MergeTree 只按它排序，不去重。不作为观察到的粒度，粒度需要人工声明并核验。
- 查询账号为 `readonly=2`（只读，可以调整查询设置），每次查询设 `max_execution_time` 与 `max_memory_usage`。错误码归类：159 为超时；60、47、81 为对象无法解析；497、164 为权限。
- 数据新鲜度：取 `system.parts` 中活动分片的最近修改时间。查询账号只授予这张表的 4 列；未授权时不报告新鲜度，不让问数失败。

**SQL Server**（M7 元数据与查询已真实实例联调；新增 DDL 归因仅离线单元测试验证；用户已确认接受 Developer Edition 的许可条款）：

- 资产名 `库.模式.表`，库放在 catalog 段（与 PostgreSQL 相同）；生成的 SQL 用三段名，行数上限写成 `TOP n`。
- 元数据采集账号只需模式上的 `VIEW DEFINITION`：读 `sys.objects`、`sys.columns`、`sys.indexes`、`sys.sql_modules`，以及 `MS_Description` 扩展属性（列注释）。读不到业务数据。
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
- 变更归因（`DDL_AUDIT`）直接读取数据库默认跟踪：从 `sys.traces` 中定位 `is_default = 1` 的跟踪，用 `sys.fn_trace_gettable` 读取 `Object:Created`、`Object:Altered`、`Object:Deleted`，只采用 `EventSubClass` 为 Commit 的已生效事件。Begin 事件及回滚事务不能作为变更证据。
- 客户需要开启 `default trace enabled`，并给采集账号授予 `ALTER TRACE`；相关诊断读取若还要求 `VIEW SERVER STATE`，也须授权。不需要 `ddl_audit_table` 选项。关闭默认跟踪时归因为 `unavailable`，原因为 `not_enabled`；读取权限不足为 `no_permission`，来源结构不符为 `incompatible`，其他读取失败为 `error` 并保留错误摘要。
- 跟踪中的 `LoginName`、`DatabaseName`、`ObjectName` 与 `StartTime` 提供对象级证据，没有可用于点名列的语句文本。列级归因沿用 [ADR 0008](0008-history-integrity.md) 的 `redefines` / `column_refs` 规则，证据不足时保留 `unmatched` / `ambiguous`。
- 默认跟踪没有历史模式名：创建、修改事件仅在当前 `sys.objects` 的对象 ID、名称、类型与创建时间均相符时恢复候选模式。这仍不能排除 `ALTER SCHEMA ... TRANSFER`，所以对象级也只列候选（`ambiguous`），不据此确定执行者；连接器以 `certain=False` 表示无法确认该事件属于目标对象的快照窗口。删除事件或无法恢复候选模式的事件保留空对象列表，归因为 `unmatched`，不猜测模式。
- `StartTime` 按 SQL Server 服务器本地时间解释，转 UTC 时使用服务器当前时区偏移；跨夏令时切换的历史事件不保证精确换算。

**Oracle**（M7 元数据与查询已真实实例联调；新增 DDL 归因仅离线单元测试验证；用户已确认接受 Oracle Free 的许可条款）：

- 资产名 `服务名.模式.表`。查询中不写服务名（`catalog_in_sql = False`），行数上限写成 `FETCH FIRST n ROWS ONLY`，执行前校验同样识别。
- **标识符大小写**：未加引号的名字在数据字典中存为大写。解析前用 `fold_identifiers("oracle")` 按 Oracle 规则折叠，视图原文的 `store.sales` 才能对上已采集的 `STORE.SALES`。这一规整只挂在 Oracle 方言上，不改变其他引擎的规范化 SQL 与指纹。
- **`DATE` 带时间部分**（方言项 `date_has_time`）：按日期过滤时，用“当日 0 点 ≤ t < 次日 0 点”的区间，而不是 `BETWEEN` 两个日期。
  - 演示库里北京那笔订单的下单时间是 10:30，用来确认这一点：用 `BETWEEN` 会漏掉它。
  - 会话固定 `NLS_DATE_FORMAT` 与 `NLS_TIMESTAMP_FORMAT`，SQL 中的日期字符串不依赖库的默认设置。
- 元数据采集账号只需 `CREATE SESSION` 与 `SELECT_CATALOG_ROLE`，读 `DBA_*` 数据字典。
  - 读业务数据会报 ORA-00942：Oracle 对无权限的对象报“不存在”，因此按“对象无法解析”归类，不改写成“权限”。
- 物理身份用 `OBJECT_ID`：`RENAME` 与 `CREATE OR REPLACE VIEW` 都不改变它。
- 视图定义取 `DBA_VIEWS.TEXT`（建视图时的原文）。
- 问数执行：
  - 在只读事务（`SET TRANSACTION READ ONLY`）内执行，并设调用超时；
  - `NUMBER` 取为 `Decimal`，默认的 float 会让金额产生精度误差；
  - 错误归类：DPY-4024 为超时；ORA-00904、00942 为对象无法解析；ORA-01031、01456、41900 为权限（ORA-41900 是 23ai 起的“缺少某项权限”）。
- 不报告数据新鲜度：`DBA_TAB_MODIFICATIONS` 依赖统计信息刷新，不能代表最近写入时间。
- 变更归因（`DDL_AUDIT`）读取 `UNIFIED_AUDIT_TRAIL`，仅采用 `RETURN_CODE = 0` 的成功 DDL；使用 `DBUSERNAME`、`ACTION_NAME`、`OBJECT_SCHEMA`、`OBJECT_NAME`，有 `SQL_TEXT` 时沿用现有解析器识别列级变化，无法确定时保留未知。
- 客户需要启用覆盖表、视图等对象 `CREATE` / `ALTER` / `DROP` 的统一审计策略，并给采集账号授予 `AUDIT_VIEWER` 等读取权限；不需要 `ddl_audit_table` 选项。没有相关记录且可从 `AUDIT_UNIFIED_ENABLED_POLICIES` 判断相关策略未启用时，原因为 `not_enabled`；无读取权限为 `no_permission`，来源结构不符为 `incompatible`，其他读取失败为 `error` 并保留错误摘要。审计查询成功但没有事件，且不能证明策略未启用时，不据此断言审计已关闭。
- 时间优先取 `EVENT_TIMESTAMP_UTC`；只有旧版明确缺少 UTC 列时，才先将审计会话的 `TIME_ZONE` 设为 `+00:00`，回退到该 UTC 会话中的 `EVENT_TIMESTAMP` 并按 UTC 解释。

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

**Hive**（2026-11-01，真实实例联调：`apache/hive:4.1.0` 的 HiveServer2，内嵌元数据库，独立 Compose 项目 `avelis-hive`，见 `deploy/demo/hive/compose.yaml`）：

- **驱动选型：impyla**（Cloudera 维护，Apache-2.0，2026-06 发布 0.24.0；依赖 thrift、thrift-sasl、pure-sasl、bitarray，Linux / Windows 都有现成 wheel，镜像构建不需要编译）。
  - 不选 PyHive：最后一次发布是 2023 年的 0.7.0，上游已不再维护。
  - 不直连 Hive Metastore（Thrift 9083）：要部署方额外开放元数据服务端口并处理它自己的认证，且拿不到视图的展开文本以外的增量信息；HiveServer2 是 Hive 对外的标准入口，权限也在这一层统一控制。
  - 不走 JDBC（JayDeBeApi + Java）：镜像要带 JRE，违背部署自包含、依赖最小的原则。
- 资产名两级 `库.表`，固定 Catalog 段 `hive`（Hive Metastore 默认 Catalog 名）；生成的 SQL 不写 Catalog 段。库、表、列名在元数据中存为小写。
- 采集只读元数据：`SHOW TABLES IN <库>` 列出表与视图，逐个 `DESCRIBE FORMATTED`。不读业务数据，不起 MapReduce / Tez 作业。配置的库在 `SHOW DATABASES` 中不可见（Hive 授权插件会按权限过滤这个列表，看不到不代表已删除）、或读不到某张表的结构时，整次采集失败，报错写明是哪个库，上一份目录原样保留；跳过会把这个库已采集的资产全部记成删除。
  - 列、类型（原样保留，如 `decimal(18,2)`、`array<string>`、`struct<a:int>`）、列注释；分区列来自 “# Partition Information”，排在普通列之后（与 `SELECT *` 列序一致），列名记在属性 `partition_columns`。
  - 表注释在 Table Parameters 的 `comment`，按 Java 转义输出（换行写成 `
`），采集时还原。
  - 表类型：`MANAGED_TABLE`、`EXTERNAL_TABLE` 都是普通表（Hive 的外部表只是数据位置由用户管理，不指向另一个数据源），原类型记在 `table_model`；`VIRTUAL_VIEW` 为视图；`MATERIALIZED_VIEW` 为物化视图。Hive 4 默认把非事务的托管表转成外部表（`TRANSLATED_TO_EXTERNAL`）。
  - 约束：主键 / 唯一约束只能 `DISABLE NOVALIDATE`（不校验），只记在属性 `declared_primary_key`，不作粒度依据；`NOT NULL` 约束（Hive 3 起强制执行）记为列不可空。
- 视图定义用 Hive 保存的**展开文本**（Expanded Query：库名、别名都已补全，与建视图时的当前库无关），用 SQLGlot 的 `hive` 方言解析列级血缘。
  - 普通视图取 `SHOW CREATE TABLE`（输出 `CREATE VIEW `库`.`视图` AS <展开文本>`），取 `AS` 之后的原文，不经 SQLGlot 重新生成，定义指纹不随 SQLGlot 版本变化。
  - 物化视图的 `SHOW CREATE TABLE` 只给建表语句、不含查询，定义取 `DESCRIBE FORMATTED` 的 “# Materialized View Information”。核心模块目前只解析普通视图的血缘（与 ClickHouse 物化视图相同）。
- 物理身份：HiveServer2 不暴露表 ID，表、视图都按名称对应；改名记为删除 + 新增，不冒充有物理身份的改名。实例身份只有版本与地址，不给 `instance_id`（不能作为外部表的追溯目标）。
- 认证（配置项 `auth`）：
  - `NONE`（默认）：HiveServer2 仍走 SASL 传输（PLAIN），但不校验口令；采集账号可不填（以 `avelis` 作会话用户），填了就以它作会话用户。
  - `NOSASL`：无 SASL 传输，账号同上。
  - `LDAP`：账号与口令必填，口令错误时采集失败。
  - Kerberos（GSSAPI）未支持：需要系统 Kerberos 库、KDC 与 keytab，镜像里没有，也没有联调。只支持 binary 传输，不支持 http 传输。
  - 引擎层为此新增两项通用能力：配置项可以是 `choice`（固定可选值 + 默认值），以及 `credential_free`（某配置项取某些值时采集账号可不填）。前端按它们渲染，不按引擎写死。
- **不支持问数执行**：Hive 查询要起计算作业，超时、资源限制、错误归类都没有联调，不声明 `query` 与 `live_check`。
- 联调中发现并修复的两个类型问题：
  - SQLGlot 的 `hive` 方言解析不了 `UNIONTYPE<…>`（上游测试里也列为“暂时解析不了”）。按约定在 fork 修：上游 issue [tobymao/sqlglot#8860](https://github.com/tobymao/sqlglot/issues/8860)、PR [tobymao/sqlglot#8861](https://github.com/tobymao/sqlglot/pull/8861)（分支 `fix/hive-uniontype`，基于上游 main）；Avelis 依赖分支 `avelis-v30.25.0`（v30.25.0 + 该修复，并去掉上游指向私有仓库的集成测试子模块，否则 uv 拉 git 依赖会失败），在 `backend/pyproject.toml` 的 `tool.uv.sources` 固定提交。上游合并发布后切回 PyPI。镜像构建阶段因此需要 git（运行镜像不含）。
  - 采集服务构造解析用 schema 时只取类型名第一个括号之前的部分，嵌套类型被截断成错的类型（如 `struct<a:decimal(10,2),b:int>` 变成 `STRUCT<A:DECIMAL`）。改为逐层去掉长度 / 精度参数、保留嵌套结构与后缀，对其他引擎的类型逐一核对过不变差。
  - SQLGlot 版本号随之变化（`PARSER_VERSION` 变化），已采集的视图会在下次采集时按新版本重新解析，这是既有机制。

## 规划中的引擎（未登记，未验证）

只考虑 SQLGlot 有方言的引擎；新引擎的前提是 SQLGlot 支持它的方言。

| 引擎 | SQLGlot 方言 | 资产名映射 | 需要实测的要点 |
|---|---|---|---|
| PostgreSQL | `postgres` | db.schema.table | `pg_views` / `information_schema.views` 的定义是否已全限定；`search_path` 对解析的影响 |

## 后果

- 新引擎只需新增适配器与验收，核心模块不改。
- 适配器的能力声明会出现在接口和页面上，用户能直接看到某种数据库支持到什么程度。
- 代价：
  - 解析、编译、校验的函数都多了一个 `dialect` 参数；
  - `parse_query`、`canonical_hash_of` 的签名变了；单元测试已同步。
- 行为不变：StarRocks 的规范化 SQL 与指纹算法没有变，`PARSER_VERSION` 不变，已有指纹不会因为这次重构失效。M1、M2 的验收全部重跑通过。
