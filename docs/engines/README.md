# 引擎核验记录

每种数据库接入前，在真实实例上逐项核验元数据从哪取、视图定义长什么样、权限与超时怎么控制，结论决定适配器的实现。新增引擎的步骤见 [ADR 0004](../decisions/0004-engine-adapters.md)。

| 引擎 | 记录 |
|---|---|
| StarRocks 3.5.14 | [starrocks.md](starrocks.md) |

PostgreSQL、ClickHouse、SQL Server、Oracle、Doris、Hive 的核验结论记录在 [ADR 0004](../decisions/0004-engine-adapters.md) 与[更多引擎验收（归档）](../archive/acceptance/acceptance-m7.md)中。
