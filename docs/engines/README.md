# 引擎核验记录

每种数据库接入前，在真实实例上逐项核验元数据从哪取、视图定义长什么样、权限与超时怎么控制，结论决定适配器的实现。新增引擎的步骤见 [ADR 0004](../decisions/0004-engine-adapters.md)。

| 引擎 | 记录 |
|---|---|
| StarRocks 3.5.14 | [starrocks.md](starrocks.md)；[可选 AuditLoader DDL 归因](starrocks.md#ddl-变更归因auditloader)，需绑定审计表并授权采集账号 SELECT |
| SQL Server 2022 Developer | [ADR 0004](../decisions/0004-engine-adapters.md)：元数据、视图血缘、问数与执行前核对已有 M7 实机核验；新增 `DDL_AUDIT` 仅离线单元测试验证。使用默认跟踪的 Commit 事件，需要启用默认跟踪并授予读取权限，无需绑定审计表；缺少历史模式名时只列候选或保留未匹配。 |
| Oracle 23ai Free | [ADR 0004](../decisions/0004-engine-adapters.md)：元数据、视图血缘、问数与执行前核对已有 M7 实机核验；新增 `DDL_AUDIT` 仅离线单元测试验证。读取统一审计中成功执行的 DDL，需要启用相关审计策略并授予读取权限，无需绑定审计表。 |

PostgreSQL、ClickHouse、SQL Server、Oracle、Doris、Hive 的核验结论记录在 [ADR 0004](../decisions/0004-engine-adapters.md) 中。

MySQL 8.4 的适配器支持元数据、视图列级血缘、只读问数、执行前核对、作为外部表的追溯目标，以及绑定审计表后的 DDL 归因。视图定义、审计启用与权限条件、UTC 时间处理和候选证据边界见 [ADR 0004 的 MySQL 说明](../decisions/0004-engine-adapters.md#mysql)。新增能力的离线单元测试与真实实例验收分别记录，不把离线测试视为生产环境联调。
