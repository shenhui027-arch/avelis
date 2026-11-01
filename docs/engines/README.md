# 引擎核验记录

每种数据库接入前，在真实实例上逐项核验元数据从哪取、视图定义长什么样、权限与超时怎么控制，结论决定适配器的实现。新增引擎的步骤见 [ADR 0004](../decisions/0004-engine-adapters.md)。

| 引擎 | 记录 |
|---|---|
| StarRocks 3.5.14 | [starrocks.md](starrocks.md) |

PostgreSQL、ClickHouse、SQL Server、Oracle、Doris、Hive 的核验结论记录在 [ADR 0004](../decisions/0004-engine-adapters.md) 中。

MySQL 8.4 的适配器支持元数据、视图列级血缘、只读问数、执行前核对、作为外部表的追溯目标，以及绑定审计表后的 DDL 归因。视图定义、审计启用与权限条件、UTC 时间处理和候选证据边界见 [ADR 0004 的 MySQL 说明](../decisions/0004-engine-adapters.md#mysql)。新增能力的离线单元测试与真实实例验收分别记录，不把离线测试视为生产环境联调。
