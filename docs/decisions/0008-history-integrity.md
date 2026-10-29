# ADR 0008：变更留底——归因到数据库用户、检索导出、防篡改与保留期

- 状态：已采纳
- 日期：2026-10-30

## 背景

元数据观察层只追加：采集到变化就新建 `asset_version`、`column_version`、`definition`；旧血缘边标为 superseded 后保留；`_diff_versions` 生成 `change_event`；人工治理操作写 `audit_event`。没有删除数据源或资产的接口。

用户决定（2026-10-30）：变更历史只在元数据与血缘治理层留底，不改问数层（不做按旧口径查询，也不做问数时的变更提示）。对留底有四项要求：

1. 能看出是数据库里哪个用户做的改动；
2. 能检索、能导出；
3. 防篡改，避免误操作；
4. 默认保留 3 年，可配置。只保留元数据和变更信息，资源占用小。

现状的缺口：

- 列注释变化会产生新版本，但不生成变更事件（`_diff_versions` 漏了这一项）；
- 变更事件只知道“在两次采集之间变了”，不知道是谁改的；
- 只能按变更集浏览，不能按对象、类型、时间、用户筛选，也不能导出；
- 历史表只是约定只追加，数据库层没有约束。拥有元数据库账号的人可以改写或删除历史而不留痕迹；
- 没有保留期，历史只增不减。

## 决定

### 1. 补齐变更类型

`_diff_versions` 新增 `column_comment_changed`，`before` / `after` 为 `{"comment": ...}`。列注释仍不进入字段推导指纹，不会让口径受影响（ADR 0006）。

### 2. 归因到数据库用户

**能力与接口。** 引擎新增能力 `ddl_audit`。声明此能力的连接器实现：

```python
ddl_events(since: datetime, until: datetime, cursors=(上次快照游标, 本次快照游标)) -> list[DdlEvent]
# DdlEvent: at（UTC）、db_user、objects [(库或模式, 对象名)]、statement_type、statement（只在内存里用）、
#           certain（连接器能按事务可见性确定时给出）、may_lag（可能晚于语句时间才生效）
```

读不到审计来源时抛 `AuditUnavailable(message, cause)`，带数据库原始报错，并由连接器按原生错误给出原因，不返回空列表冒充“没人改”。连接器还要在读取前核对审计来源确实在工作（PostgreSQL：两个事件触发器存在、已启用、指向预期函数；Doris：`enable_audit_plugin = true`），否则视为未开启——停用后留下的旧记录不能再当作确定依据。

**审计来源（先实测再声明）：**

| 引擎 | 来源 | 结论 |
|---|---|---|
| PostgreSQL | 事件触发器（`ddl_command_end` + `sql_drop`）写入 `avelis_audit.ddl_event`；部署脚本放在演示库 `deploy/demo/shop-pg/init/03-ddl-audit.sql` | **接入**。记录 `session_user`（登录用户）、命令标签、对象地址（`pg_identify_object_as_address`）、`current_query()` 与事务号 `xid`（`pg_current_xact_id()`）；级联删除的表与视图也记到执行人名下；`(at, id)` 与 `xid` 各有索引，模式与对象类型过滤在库里完成。触发器函数为 SECURITY DEFINER，执行 DDL 的人不能伪造审计记录。采集账号只授予该表的 SELECT |
| ClickHouse | `system.query_log`（默认开启） | **接入**。只取 `type = 'QueryFinish'`、`query_kind ∈ {Create, Alter, Drop, Rename}`，对象取 `tables` 列。采集账号只授予六列的 SELECT，并用行策略限制为只见 DDL 行（看不到查询语句）。日志默认每 7.5 秒落盘一次 |
| Doris | `__internal_schema.audit_log`（4.1 默认 `enable_audit_plugin = true`） | **接入**。只取 `stmt_type ∈ {CREATE, ALTER, DROP}`、`state = 'OK'`；DDL 的 `queried_tables_and_views` 为空，对象从语句中解析。审计批量写入，最长延迟 `audit_plugin_max_batch_interval_sec`（默认 60 秒）。采集账号需该表的 SELECT_PRIV（Doris 不支持行级限制，能看到全部语句，审计表里口令已被 Doris 遮蔽） |
| StarRocks | AuditLoader 插件（需另装插件、建表） | 未接入：演示环境未装插件，未实测 |
| SQL Server | 默认跟踪（`fn_trace_gettable`）或 Extended Events | 未接入：读取需要 `ALTER TRACE` / `VIEW SERVER STATE`，权限远超只读采集账号，留待评估 |
| Oracle | 统一审计 `UNIFIED_AUDIT_TRAIL` | 未接入：需要建立并启用审计策略，读取需 `AUDIT_VIEWER`，未实测 |
| MySQL | general log / 审计插件 | 未接入 |

**窗口与确定性。** 每次采集记下读快照的开始与结束时间 `[C_start, C_done]`（存在 `collection_run.stats.snapshot_window`）。与上次的 `[P_start, P_done]` 一起：

- **确定在窗口内**：生效时间在 `(P_done + 容差, C_start − 容差)`；容差 `AVELIS_DDL_AUDIT_SLACK_SECONDS`，默认 5 秒，只用于两侧时钟偏差。
- **边界候选**：落在两次快照读取期间（± 容差）的语句；可能异步生效的语句（Doris 的 `ALTER`，`may_lag`）在 `P_start` 之前 `ddl_effect_lag_seconds`（Doris 300 秒）内也算。
- **按事务确定（PostgreSQL）**：快照在 `REPEATABLE READ, READ ONLY` 事务里读，同时记下 `pg_current_snapshot()`；审计只取“上次快照看不到、本次快照看得到”的事务（`pg_visible_in_snapshot(xid, …)`），结果确定。跨采集提交的长事务因此归到它提交的那个窗口，不会被执行时间截掉，也不会把同列上另一个人的改动错当成唯一候选。上次采集没有记下游标时（升级后第一次）退回按时间判断，这时的候选一律算边界候选，不给确定归因。
- **游标与实例绑定**：游标记下实例键：有可信的 `instance_id`（PostgreSQL `system_identifier`）时用它，没有时用（端点、`pg_postmaster_start_time()`、库 oid），实例重启或切换都会让键变化；读审计的连接再核对一次实例键，与本次快照不同或无法确认都按实例已切换处理。实例切换（身份世代变化，或两次游标的实例不同）的那一轮不归因，全部记为 `unavailable / instance_changed`（“数据源实例已切换，本轮重新建立归因基线”），下一轮从新实例的游标开始。

**关联规则（不猜测）。** 把审计事件按对象名关联到本次的每个直接变更事件：

- 对象：审计事件的对象（库或模式 + 名称）与资产当前名称**精确相等**（区分大小写：PostgreSQL 的 `store."Orders"` 与 `store.orders` 是两个对象；连接器负责给出目录里的真实写法）；改名事件同时匹配旧名与新名。
- 字段级变化（`column_*`）：候选 = 在列的**语法位置**上点名了该列的语句 ∪ 整体重定义（`CREATE …`、`ALTER VIEW … AS`，不一定逐列点名）∪ 针对该对象、但识别不出涉及哪些列的语句。认得的写法：`ALTER TABLE / VIEW / MATERIALIZED VIEW / FOREIGN TABLE <对象>` 的 `ADD / DROP / ALTER / MODIFY [COLUMN] <列>`、`RENAME [COLUMN] <旧> TO <新>`（PostgreSQL 可省略 COLUMN）、`CHANGE [COLUMN] <旧> <新>`、Doris 的 `ADD COLUMN (a …, b …)`、ClickHouse 的 `COMMENT / CLEAR / MATERIALIZE COLUMN <列>`、主键与 `NOT NULL` 约束点名的列、`COMMENT ON COLUMN <表>.<列>`；确定不改列结构的写法（改属主、`SET SCHEMA`、改表名、`ADD CHECK / UNIQUE / FOREIGN KEY` 约束、加减索引与分区、表注释）不是候选。**原则：针对同一对象、但识别不出列位置的语句（如 `DROP CONSTRAINT`、未知的 `ALTER TABLE` 动作、词法上无法确定的记录）视为可能涉及任何一列——它是候选，但只能得到 `ambiguous`，不能因为识别失败就让另一个用户被确定归因。**多语句时只看目标是该对象的那几条。词法跳过字符串、注释（含 PostgreSQL 的嵌套块注释）与 `$tag$` 美元引用，支持中文等 Unicode 标识符，标识符按引擎规则比较（PostgreSQL 不带引号折小写）。同一对象上只改了别的列、或只在注释值、约束名里出现该词的语句**不算**——那是猜测，这种情况记为 `unmatched` 并说明。
- 结论：
  - `matched`：候选只来自**唯一一个**数据库用户，且**全部**确定在窗口内 → 记为该用户；
  - `ambiguous`：候选来自多个用户，或**存在任何**边界候选或识别不出列位置的候选（哪怕同一用户另有确定候选）→ `actor` 为空，列出全部候选用户和语句，由人判断；
  - `unmatched`：窗口内没有该对象的审计记录 → `actor` 为空，说明原因（例如审计日志尚未落盘、改动发生在审计开启之前）。
- 另有三种“无法归因”：`unsupported`（引擎未接入审计来源）、`unavailable`（声明了能力但本次读取失败，附报错）、`not_applicable`（推导出的变化，如字段推导变化、上游缺失、身份映射变化，不是对该对象执行的 DDL）。
- `unavailable` 按连接器给出的原生错误细分原因 `cause`：`no_permission`（采集账号无权读取 DDL 审计）、`not_enabled`（数据源未开启 DDL 审计：审计表不存在、事件触发器停用、审计插件关闭）、`incompatible`（审计来源结构与连接器要求不符，如缺列）、`error`（其他，如网络错误）。是否开启审计、是否给采集账号授权由数据源方负责（用户决定，2026-10-30）：产品只如实说明该功能不可用及原因，不做兜底或变通。

变更事件新增三列，在插入时写定，之后不再修改：

| 列 | 含义 |
|---|---|
| `actor_db_user` | 数据库用户；只有 `matched` 时有值 |
| `attribution_status` | 上述六种之一 |
| `attribution` | 来源、窗口、匹配依据、候选语句的结构化信息（时间、用户、命令类型、是否确定、事务号；不含语句原文）、原因 |

**不落库语句原文。** 候选语句只保存审计来源给出的结构化信息：时间、数据库用户、命令类型、是否确定，以及事务号（PostgreSQL `xid8`）；对象已在归因依据里。任何语句原文或摘要都不落库（变更集详情、检索、导出都看不到）：多语句请求、各引擎的词法差异（如 PostgreSQL 的 `ARRAY[…]`、嵌套注释、美元引用）让“哪段文字能安全保存”无法可靠判断，宁可不存。原文只在内存里用于列匹配，用完即丢；词法器只服务于列匹配，不承担安全判定。审计读取异常与归因中的任何异常都只影响归因（相关事件记为 `unavailable / error` 并写明原因），不让采集失败。

### 3. 检索与导出

- `GET /api/change-events`：按数据源、资产、字段、变更类型、时间段、数据库用户、归因状态筛选，分页返回（`limit` ≤ 200，`offset`），附总数。只返回当前用户可见资产的事件（与变更集详情同一规则；无资产的事件只对管理员可见）。
- `GET /api/change-events/export?format=csv|json`：同一组筛选条件，不分页。在独立的 `REPEATABLE READ` 只读事务里读链头与事件（同一快照），数据查询带 `cap + 1` 硬限制（`AVELIS_HISTORY_EXPORT_MAX_ROWS`，默认 50000），超出返回 413 并提示缩小范围；导出审计另写。每行带该事件的链上序号 `chain_seq` 与哈希 `row_hash`；文件附导出清单：筛选条件、行数、导出时间、导出人、内容 SHA-256、导出时链头（序号与哈希）。
  - CSV：UTF-8 带 BOM（Excel 直接打开中文）；以 `= + - @` 或控制字符开头的文本单元格前加 `'`，防止被当作公式（JSON 保留原值）；清单放在响应头 `X-Avelis-Export-Rows / -Sha256 / X-Avelis-Chain-Head` 与文件末尾以 `#` 开头的行。内容哈希 = 实际写出的、去掉 `#` 行后全部字节（含 BOM）的 SHA-256。
  - JSON：`{"manifest": …, "events": […]}`；内容哈希 = `events` 按键排序、紧凑序列化（`separators=(',', ':')`，不转义非 ASCII）后的 SHA-256。
  - 事后核对：重算内容哈希与清单比对；拿行哈希与元数据库中同一 `chain_seq` 的行比对，再跑一次核验。导出写审计（`history.export`）。
- 前端：变更页新增“记录”视图，筛选 + 表格 + 导出按钮；变更集详情的事件表显示数据库用户与归因依据。

### 4. 防篡改

**只追加（数据库层）。** 以下表加 `BEFORE UPDATE / DELETE` 行触发器与 `BEFORE TRUNCATE` 语句触发器：

| 表 | 允许的修改 |
|---|---|
| `asset_version` | `superseded_at` 空 → 非空一次；`last_seen_at` / `last_seen_run_id` 只能前移（“最近一次看到”是存活标记，不是历史内容，不进入哈希） |
| `column_version`、`definition`、`change_event`、`audit_event`、`metric_version_transition`、`history_mark`、`history_anchor` | 不允许 |
| `lineage_edge` | `status` active → superseded 与 `superseded_at` 空 → 非空，**必须同时**、只一次；已取代的边完全冻结 |
| `change_set` | `status` open → resolved 与 `resolved_at` 空 → 非空，**必须同时**、只一次 |

状态列作为一组先整体校验，只改其中一列（制造 `active + 非空 superseded_at` 这类无效状态）会被拒绝。其他列一律不能改。尚未封链的行只在**插入它的事务里**可以补全业务字段（如 `change_set.summary`），主键冻结。“插入它的事务”以行里的 `inserted_xact`（`xid8`）为准：插入触发器强制写入 `pg_current_xact_id()`（保存点内返回的也是顶层事务号，64 位不回卷），之后任何时候都不能改；未封链的行只有在 `inserted_xact = pg_current_xact_id()` 时才能补全，`INSERT … ON CONFLICT DO NOTHING` 不会改动已有行的事务号，调用者也无法伪造；已提交却未封链的行（绕过触发器写入的）不能再改。封链时找不到本事务插入的行、链头不存在或写回失败，**拒绝提交**。DELETE 只在清理专用路径（第 5 点）中放行；TRUNCATE 一律拒绝。违反时报 `avelis_history_append_only`，并说明表、列与规则。DDL（`DROP TABLE`）不在拦截范围内：生产部署应让应用账号不是表的属主。

**哈希链。** 上述表各加三列 `chain_seq`、`prev_hash`、`row_hash`（另有 `inserted_xact`，进入内容摘要），组成**一条全局链**（跨表按 `chain_seq` 排序）：

- 封链在**提交时**进行：`DEFERRABLE INITIALLY DEFERRED` 的约束触发器在事务提交前，对本事务插入的每一行锁住全局链头 `history_chain_head`，取下一个序号，计算
  `row_hash = sha256(prev_hash | chain_seq | 表名 | content_digest)`，
  `content_digest = sha256((to_jsonb(行) − 链列 − 可变列)::text)`，再写回本行。
- 只在提交时持锁，锁持有时间是“封本事务的行 + 提交”，不是整个采集事务；只有一把链锁，不会因加锁顺序不同而死锁。回滚的事务不占序号，链不会因回滚出现空洞。
- 可变的状态列（`superseded_at`、`status` 等）不进入 `content_digest`；它们的每一次合法变化由触发器追加一条 `history_mark`（表、行、列、新值），`history_mark` 本身在链上。核验时要求行上的状态列与最后一条标记一致：绕过触发器改状态列会被发现。

**核验。** `uv run python -m avelis.bootstrap verify-history` 与 `GET /api/history/verify`（管理员）：

- 按 `chain_seq` 顺序重算每行的 `content_digest` 与 `row_hash`，检查 `prev_hash` 与上一行相接；空链只有在链头仍是起点（序号 0、64 个 0）时才合法；
- 序号空洞必须被清理锚点（第 5 点）登记过，且锚点记录的前后哈希与两侧相符；
- 已提交却未封链的行（例如绕过触发器插入）、状态列与标记不符，都报告；
- 报告第一处断链的位置（序号、表、行 ID、原因）以及全部问题列表（上限 100 条），不修复、不重写。核验结果写审计。

**迁移补算。** 迁移按表的自然时间顺序为已有数据逐行补算哈希，之后再启用触发器。降级先拆触发器与函数，再删新增的表与列，原业务历史保留；已在含数据的库上做过“升级 → 降级 → 再升级”的测试。

**威胁模型。** 触发器防误操作和普通账号的直接改写；哈希链让绕过触发器（超级权限、`session_replication_role = replica`）的改写可被发现。能重算整条链的人（掌握数据库超级权限并了解算法）可以伪造一条自洽的新链：导出文件与核验接口都给出链头（序号 + 哈希），运维可把链头定期记到库外（工单、对象存储）作为外部锚点，后续核验时比对。v0.1 不引入密钥签名。

### 5. 保留期

- `AVELIS_HISTORY_RETENTION_DAYS`（默认 1095，即 3 年；`0` 表示不清理）。worker 每 `AVELIS_HISTORY_PURGE_INTERVAL_HOURS`（默认 24）小时执行一次；也可以 `uv run python -m avelis.bootstrap purge-history` 手动执行。
- 超期的定义（截止时间 = 现在 − 保留天数）：

| 对象 | 超期条件 | 一定保留 |
|---|---|---|
| `asset_version`（连同其 `column_version`、`definition`、`parse_result`） | `superseded_at` 早于截止时间 | 资产的当前版本（`asset.current_version_id`，含已删除资产的最后一版）；被非终态口径版本（草稿、待复核、已发布、受影响）的 `asset_versions` 引用的版本，以及这些版本中派生口径 `components[].version_id` 固定引用的组成版本（组成版本可能已归档）的 `asset_versions`；仍被未取代的血缘边或粒度引用的版本 |
| `lineage_edge` | 已取代且 `superseded_at` 早于截止时间 | 未取代的边 |
| `change_set`（连同 `change_event`、`impact`、`review_task`） | 已处理（resolved）且 `window_end` 早于截止时间，且其复核任务全部已处理 | 未处理的变更集 |
| `audit_event` | `at` 早于截止时间 | — |
| `metric_version_transition` | `at` 早于截止时间 | 每个口径版本的最后一条（解释当前状态） |
| `history_mark` | 所属行已被清理 | — |
| 推导层历史（已取代的 `column_fingerprint`、`identity_link`、`grain`）、不再被引用的 `collection_run` | `superseded_at` / `finished_at` 早于截止时间 | 未取代的记录 |

- **专用路径：** 删除事务内执行 `SET LOCAL avelis.history_purge = 'on'`，触发器只在该标志下放行逐行 DELETE（UPDATE、TRUNCATE 仍然拒绝）。标志防的是误操作，不是权限边界。
- **两段式，先核验、后清理：** ① 在 `REPEATABLE READ` 只读快照里核验全链、挑出超期的行、算好空洞；链不完整就不清理，把核验结果写审计并返回失败。② 删除事务先对决定保留资格的表（`asset`、`metric_version`、`lineage_edge`、`grain`、`review_task`）加 `SHARE` 锁（`NOWAIT`：拿不到就稍后整轮重跑，不与其他事务互等，不会造成死锁），在锁下按同样的规则重新挑一遍；计划里有行在两段之间重新获得了保留资格（例如被驳回的口径改回草稿，仍引用旧版本）就整轮重跑，否则按 ID 删除、写锚点。删除事务内 `lock_timeout = 5s`、`statement_timeout = 120s`，超时回滚并稍后整轮重跑。**清理期间（拿到锁到提交，通常数秒）会短暂挡住这五张表的写入**（资产心跳与当前版本指针、口径状态流转、血缘与粒度、复核任务），等待的写入在清理提交或超时回滚后继续。实际删除行数或序号与计划不符（有人绕过触发器动过）时整体回滚。超期的历史行本身已冻结，链尾的并发追加不影响①的结论，持续写审计也不会让清理失败；遇到序列化失败、死锁、锁不可得或语句超时时整轮重跑（最多 5 次）。被驳回的口径改回草稿时，若它引用的资产版本已按保留期清理，草稿不再绑定旧版本（清空 `asset_versions`，重新提交时按当前版本绑定），不留悬空引用。同一时刻只允许一个清理（会话级咨询锁）。
- **清理锚点：** 清理在链上追加一条 `history_anchor`，记录截止时间、各表删除行数，以及因本次清理新形成的每个序号空洞：`{from_seq, to_seq, before_hash（空洞前一行的 row_hash，链首为 64 个 0）, last_hash（空洞最后一行的 row_hash）}`。核验遇到空洞时要求某个锚点登记了**同一**区间且两端哈希相符，所以剩余的链仍可核验，未经清理路径的删除仍会被发现。
- 清理本身写 `audit_event`（`history.purge`），包括截止时间、行数与锚点 ID。

### 6. 范围

- 不改问数层：问数仍只看当前结构，不按旧口径查询，也不做变更提示。
- 不新增删除数据源 / 资产的接口。
- 归因只覆盖本次采集与上次采集之间的审计记录；不回溯补算历史变更事件的归因（事件只追加，不改写）。

## 后果

- 每次提交多一次链头锁和每行一次回写：采集写入数千行时提交阶段多出约数百毫秒；不同事务的提交在封链阶段串行。
- 迁移 `0007` 在已有数据上补算哈希；M8a 的迁移 `0006` 合并后，本迁移的 `down_revision` 需改为 `0006`（谁后合并谁调整）。
- 审计来源有延迟（ClickHouse 约 7.5 秒、Doris 最长 60 秒）：DDL 与采集贴得太近时归因为 `unmatched` 或 `ambiguous`（边界候选），原因中注明。归因不重算，事件如实保留“未知”。
- 已知边界：PostgreSQL 事件触发器在窗口中途被停用又启用，停用期间的改动没有记录、采集时也看不出来，这类改动可能被同列上已审计的另一条语句“唯一候选”归因；ClickHouse 只能按时间判断（DDL 非事务），无法发现会话级关闭 `log_queries`。
- 采集账号多出读取审计来源的权限：PostgreSQL 一张审计表、ClickHouse `system.query_log`、Doris `__internal_schema.audit_log`；仍然读不到业务数据。
- `DROP TABLE` 等 DDL 不被触发器拦截；`reset_metadata_db`（开发工具）仍可重建元数据库。
