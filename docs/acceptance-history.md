# 变更留底验收（ADR 0008）

用户决定（2026-10-30）：变更历史只在元数据与血缘治理层留底，不改问数层。本次验收在真实演示库上检查四项要求：能看出数据库里哪个用户做的改动、能检索导出、防篡改、保留期可配置。

```bash
cd backend
uv run pytest -m acceptance tests/acceptance/test_m12_history.py
```

需要更多引擎的演示库（`uv run scripts/demo.py up --engines`）。验收会重置演示库与元数据库；如果其他会话正在用同一套演示环境，先确认它空闲。

## 场景与断言（`test_m12_history.py`）

| # | 场景 | 断言的实际行为 |
|---|---|---|
| 01 | PostgreSQL：临时账号 `avelis_dev`（`store.sales` 的属主）加列，`postgres` 改列注释 | `column_added sales.channel` 归因 `avelis_dev`，依据是事件触发器记录的 `ADD COLUMN channel` 语句；`column_comment_changed shops.city` 的 before / after 为注释，归因 `postgres` |
| 01 | 关掉事件触发器后再改注释 | `unmatched`，`actor` 为空，原因写明窗口内没有涉及该列的 DDL（同一张表上别的列的语句不算） |
| 01 | 撤销采集账号对审计表的 SELECT 后再改注释 | 采集仍成功；事件为 `unavailable`，原因带数据库原始报错 `permission denied` |
| 01 | `postgres` 执行 `CREATE OR REPLACE VIEW`（加一列） | `definition_changed` 与视图的 `column_added` 都归因 `postgres`（整体重定义可作为字段级候选）；旧血缘边被取代并保留 |
| 02 | ClickHouse：临时账号加列，`default` 改列注释（`SYSTEM FLUSH LOGS` 后采集） | 分别归因 `avelis_dev`、`default`，来源 `clickhouse:system.query_log`；采集账号经行策略只看得到 DDL 行 |
| 03 | Doris：临时账号加列，`root` 改列注释（等审计落盘，最长 60 秒） | 分别归因 `avelis_dev`、`root`，对象从语句中解析 |
| 04 | StarRocks（未接入审计）改列注释 | `unsupported`，`actor` 为空，原因“未接入 DDL 审计来源”；`/api/engines` 中只有已接入的引擎声明 `ddl_audit` |
| 05 | 检索 | 按数据源 + 数据库用户筛到唯一一条；按类型筛选、分页不重叠；按归因结论筛“未知”；查询者只看到可见资产的事件，用户下拉也只列这些事件里的用户 |
| 05 | 导出 | CSV、JSON 与同条件检索的行完全一致；CSV 去掉 `#` 清单行后的 SHA-256 等于响应头 `X-Avelis-Export-Sha256`；JSON 的 `events` 规范化后的 SHA-256 等于清单；每行 `row_hash` 与元数据库一致；导出写审计 |
| 06 | 用应用账号直接改历史表 | `UPDATE change_event`、`DELETE audit_event`、`TRUNCATE column_version`、改 `definition`、改口径流转记录都被拒绝（`avelis_history_append_only`）；`superseded_at` 不能再改、不能改回空；已取代的血缘边不能改回有效、不能改表达式；已处理的变更集不能改回待处理；不能改链列。合法的状态变化留有 `history_mark` |
| 07 | 用超级权限（`session_replication_role = replica`，绕过触发器）篡改 | 改 `actor_db_user`：核验第一处问题为 `content_mismatch`，序号、表、行 ID 正是被改的那行；改 `superseded_at`：`mark_mismatch`；删一行审计：`gap_unregistered`，指出缺失的序号。每次恢复原值后核验恢复完整 |
| 07 | 链不完整时清理 | 拒绝清理（`PurgeRefused`），并写审计 `history.purge_refused` |
| 08 | 保留期 | 3 年保留期下没有可清理的历史；模拟两天后、保留期 1 天：删除超期的版本、已取代的血缘边、已处理的变更集、审计等；当前版本、被已发布口径引用的版本、未处理的变更集都保留；清理写审计（行数与锚点一致）；核验仍完整（1 个清理锚点）；清理后再篡改一行，核验仍能指出位置 |

单元测试 `tests/unit/test_history.py`：DDL 对象解析、口令遮蔽、列名点名与整体重定义的判断、六种归因结论（含窗口外记录不参与、读取异常归为 `unavailable`）、哈希链核验（改内容、删行、链头不符）、多次清理后空洞合并且剩余链仍可核验。

## 开发库上的数据库级测试（`tests/metadb/`，标记 `db`）

不 seed、不重置共用的演示库：只在库名以 `_dev` / `_test` 结尾的元数据库，以及同一实例上它的 `<库名>_src` 源库上运行；未设置时整组跳过。

```bash
cd backend
AVELIS_DATABASE_URL=postgresql+psycopg://avelis:…@127.0.0.1:15432/avelis_hist_dev uv run pytest -m db tests/metadb
```

| 测试 | 覆盖（PR #9 审计编号） |
|---|---|
| `test_history_db.py` | 未封链行主键冻结、已提交未封链行不可改、封链找不到行拒绝提交（#2）；状态列成对改（#9）；清理开关下 TRUNCATE 仍被拒（#10）；导出同一快照、`cap + 1` 上限、CSV 防公式与按实际字节算哈希（#12、#13）；派生口径固定引用的组成版本不被清理（#3）；持续写入时清理成功且链完整（#14）；删光链上记录但链头未动会被发现（#11）；含数据的库“升级 → 降级 → 再升级”（迁移） |
| `test_postgres_audit.py` | 跨采集提交的长事务归到它提交的窗口（#5）；事件触发器停用时为“未开启”（#4）；审计表缺列为 `incompatible`、表不存在为 `not_enabled`（#18）；多语句请求不落库原文（#1）；审计表的时间与事务号索引（#15） |

## 各引擎的审计来源

| 引擎 | 结论 | 说明 |
|---|---|---|
| PostgreSQL 18 | 接入，已运行验证 | 事件触发器 `deploy/demo/shop-pg/init/03-ddl-audit.sql`（需超级用户部署）；采集账号只读 `avelis_audit.ddl_event` |
| ClickHouse 26.8 | 接入，已运行验证 | `system.query_log`；采集账号六列 SELECT + 行策略 `avelis_ddl_only` |
| Doris 4.1 | 接入，已运行验证 | `__internal_schema.audit_log`；采集账号该表 SELECT_PRIV |
| StarRocks 3.5 | 未接入 | 需 AuditLoader 插件，演示环境未装 |
| SQL Server 2022 | 未接入 | 默认跟踪 / Extended Events 需要 `ALTER TRACE` / `VIEW SERVER STATE` |
| Oracle 23ai | 未接入 | 统一审计需要审计策略与 `AUDIT_VIEWER` |
| MySQL 8.4 | 未接入 | — |

## 最近一次运行

（见下节，由全量 `uv run scripts/acceptance.py` 填写。）
