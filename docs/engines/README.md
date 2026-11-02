# 引擎核验记录

每种数据库接入前，在真实实例上逐项核验元数据从哪取、视图定义长什么样、权限与超时怎么控制，结论决定适配器的实现。新增引擎的步骤见 [ADR 0004](../decisions/0004-engine-adapters.md)。

| 引擎 | 记录 |
|---|---|
| StarRocks 3.5.14 + AuditLoader 4.2.2 | [starrocks.md](starrocks.md)：2026-11-01 已实机验证 [DDL 归因](starrocks.md#ddl-变更归因auditloader)，需绑定审计表并授权采集账号 SELECT；FE 时区为 UTC，非 UTC 换算未实机验证；插件刚安装后的几十秒内可能缺少记录，归因为未匹配。 |
| MySQL 8.4.10 | [ADR 0004 的 MySQL 说明](../decisions/0004-engine-adapters.md#mysql)：2026-11-01 已实机验证完整适配器与绑定审计表后的 DDL 归因；`general_log` 也记录执行失败的 DDL，因此只能作候选证据。 |
| SQL Server 2022 Developer | [默认跟踪与写盘滞后](sqlserver.md)、[ADR 0004](../decisions/0004-engine-adapters.md)：元数据、视图血缘、问数与执行前核对已有 M7 实机核验；默认跟踪 DDL 归因于 2026-11-01 实机验证。需要启用默认跟踪并授予读取权限，无需绑定审计表；采用 10 秒写盘滞后容差，近期窗口无候选时保留歧义。缺少历史模式名时只列候选或保留未匹配。演示实例时区偏移为 0，本地时间换 UTC 与跨夏令时未实机验证。 |
| Oracle 23ai Free | [ADR 0004](../decisions/0004-engine-adapters.md)：元数据、视图血缘、问数与执行前核对已有 M7 实机核验；统一审计 DDL 归因于 2026-11-01 实机验证，使用自建策略（CREATE / ALTER / DROP TABLE、CREATE / DROP VIEW、COMMENT、RENAME、TRUNCATE TABLE）与 `AUDIT_VIEWER`。无需绑定审计表；无记录且无策略的 `not_enabled` 分支未实机验证。 |

PostgreSQL、ClickHouse、SQL Server、Oracle、Doris、Hive 的核验结论记录在 [ADR 0004](../decisions/0004-engine-adapters.md) 中。

以上 2026-11-01 的实机结论来自演示环境真实实例验证；具体版本与未覆盖边界按表中记录，不推及其他版本或生产环境。MySQL 与 StarRocks 均须显式绑定 `ddl_audit_table`；留空表示没有审计来源，归因状态为 `unsupported`，原因明确说明“未绑定审计表”。
