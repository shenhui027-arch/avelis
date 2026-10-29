# M4 验收说明：异常与加固

- 日期：2026-10-29
- 完成标准：每个异常场景都在真实环境中制造，断言实际行为（是否执行 SQL、返回值、数据库状态、事件与审计），不只看状态码。
- 环境：
  - 本机 Docker 演示环境：StarRocks 3.5.14、MySQL 8.4 维表库、PostgreSQL 18、Valkey 8。
  - 问数理解使用演示规则（确定性）。M1 中的两项真实 DeepSeek 用例在回归中一并重跑。
- 测试文件：[backend/tests/acceptance/test_m4_exceptions.py](../backend/tests/acceptance/test_m4_exceptions.py)。

## 怎么复现

```bash
uv run scripts/demo.py up
cd backend
uv run pytest -m acceptance tests/acceptance/test_m4_exceptions.py   # 只跑 M4
uv run pytest -m acceptance                                          # M1 + M2 + M4 全部
```

验收会把演示库（StarRocks 与 MySQL 维表库）、元数据库、Redis 中的 `avelis:*` 键全部重置到基线。从 M4 起，`demo.py seed` 也会重建 MySQL `shop_dim`。

## 结果

| 运行 | 结果 |
|---|---|
| 单元测试 `uv run pytest` | 34 passed |
| M4 验收 | 12 passed（26 秒） |
| 全部验收 `pytest -m acceptance` | 33 passed（61 秒；含真实 DeepSeek 两项） |
| 前端 `tsc -b` 与 `vite build` | 通过 |

## 场景与实际行为

| # | 场景 | 制造方式 | 断言的实际行为 |
|---|---|---|---|
| 00 | 准备 | `demo_setup` 经 API 建用户、业务域、数据源并采集；另建无业务域用户 qian；发布实付销售额、订单数、类目销售额（跨域关联） | 三个指标真实执行：300.00、华东订单数、270.00 |
| 01 | 越权：检索、血缘图、结果、缓存 | sun 只属于交易域；qian 不属于任何业务域；王强的类目销售额答案已缓存 | sun 按 ID 访问商品域资产得到 404；资产列表和血缘图都不含商品域节点。qian 资产列表为空，问数为 `clarify`，没有 SQL。sun 问同一问题得到 `denied/permission`：SQL 为空、结果为空、`cache_hit=false`。sun 读不到王强的答案（404）。王强再问命中缓存，证明缓存确实存在 |
| 02 | 伪造意图、注入取值 | 绕过模型，直接把意图交给确定性管线 | 引用不可见的指标：`invalid_intent`。指标可见但来源无权：`permission`。使用未开放的维度：`invalid_intent`。以上三种都没有 SQL。维度取值 `华东' OR '1'='1` 只会成为一个转义后的字符串常量，真实执行结果为 0；`shop.orders` 行数不变 |
| 03 | 提示注入 | 问题夹带“DROP TABLE shop.orders”；MySQL 表注释写入恶意指令 | 意图仍是“订单数”。执行的是单条确定性 SELECT，不含 DROP，数值与正常提问一致。注释原样记为 `comment_changed` 事件的数据。表未被删除，三个指标状态不变 |
| 04 | 不支持的 SQL | `UNION ALL` 视图 | 解析状态 `unsupported`，原因含 UNION。各列指纹均为 `unknown`（证据不全）。基于它的指标提交时 409，阻断原因含“证据不全”和 UNION；版本仍为草稿 |
| 05 | 查询超时 | 每行 `sleep(3)` 的视图；问数超时设为 2 秒 | StarRocks 真实返回 5024，答案为 `error/db_timeout`，消息保留原始报错。有 SQL 和执行时间，结果为空。连问两次都没有缓存，每次都在 15 秒内返回 |
| 06 | 同名对象 | 改名；删除后同名重建；`ads.orders` 与 `shop.orders` 同名 | 改名后资产 ID 不变，事件 `renamed` 的依据是“TABLE_ID 未变”。删后重建得到新资产，旧资产标记 `deleted`，人工声明的粒度不继承，事件有 `deleted` 和 `added`。跨库同名是两个资产：导入不带库名时报“缺少库名”且不写边；`USE ads` 后只解析到 `ads.orders` |
| 07 | 故事 7：删除被依赖的列 | `ALTER TABLE shop.orders DROP COLUMN paid_amount` | **元数据未刷新时**，新问题真实执行，StarRocks 报 1064，答案为 `error/db_invalid_object`，不改写成成功或空结果；订单数照常回答。**采集后**：<br>• 事件 `column_removed`、`upstream_broken`；<br>• 实付销售额 V1 进入 `affected`，漂移状态为 `broken`；<br>• 与之前完全相同的问题被拦截：`metric_affected`，无 SQL，`cache_hit=false`；<br>• 订单数、类目销售额照常回答，状态仍为已发布 |
| 08 | 外部表来源变化 | MySQL 删除 `dim_region.provinces`；MySQL 把 `dim_category.category_name` 从 `VARCHAR(32)` 改为 `VARCHAR(64)`；都只采集 MySQL | **删列**：事件有 `column_removed` 与外部表的 `identity_link_changed`。`dim.ext_region` 映射变为 `mismatch`，`missing_in_source` 含 provinces。下游 `ads.region_board` 出现在影响中。<br>**改类型**：事件 `column_type_changed`。类目销售额经 Catalog 表受影响，被拦截，没有 SQL；订单数不受影响 |
| 09 | worker 进程 | 用子进程启动 `python -m avelis.jobs`。任务有三种：经 Redis 唤醒的采集；只写在 job 表里、没有入队的任务；一个“运行中”但已超过 2 小时的任务 | 前两个任务都变为 `succeeded`，结果里的 run_id 在采集记录中。运行中的旧任务被记为 `failed`，原因写明“未自动重跑”。worker 日志包含启动信息和该任务 ID |
| 10 | 智能体会话存储丢失 | `DROP SCHEMA avelis_agent CASCADE` 后继续问数 | 回答正常；`avelis_agent` 被自动重建 |
| 11 | 未归属业务域的资产 | 新建表，不归属业务域，在其上发布指标 | 默认策略 `deny`（`/api/settings` 可见）：维护者能看到并负责归属；分析师按 ID 访问为 404，列表中没有；问数被拒绝，原因含“未归属业务域”，没有 SQL。改为 `allow` 后分析师可见，真实执行得到 5.00 |

## M4 中的改动

**后端加固**

- **未归属业务域的资产默认拒绝。**
  - `AVELIS_UNASSIGNED_ASSET_POLICY=deny|allow`，默认 `deny`：只有管理员与维护者可见，由他们负责归属。
  - 拒绝原因会写明“未归属业务域”。
  - M1 用例与 `demo_setup` 改为把全部演示资产都归属到业务域。
- **智能体会话存储被删后自动恢复**：读会话状态时遇到“表或模式不存在”，重建检查点表后继续。原会话的追问状态不可恢复。
- **worker**：
  - 轮询与积压扫描间隔可配置：`AVELIS_WORKER_SCHEDULE_SECONDS`、`AVELIS_WORKER_STALE_SECONDS`。
  - 修复一个缺陷：worker 在执行中退出后，任务永远停在 `running`。`schedule_due` 又把“已有任务在跑”的数据源跳过，所以该数据源的定时采集会永久停止。
  - 现在运行超过 `AVELIS_WORKER_RUNNING_TIMEOUT_SECONDS`（默认 3600）的任务会被如实记为失败，不自动重跑。

**引擎适配层**：多数据库扩展，见 [ADR 0004](decisions/0004-engine-adapters.md)。

- 解析核心与具体数据库解耦；方言与能力来自注册表。
- 新增 `GET /api/engines`。
- 前端“添加数据源”按引擎能力渲染。

**演示环境**：`demo.py seed` 同时重建 MySQL 维表库。

## 尚未验证

- worker 长时间（数小时以上）运行：本次只验证了单次启动内的唤醒、扫描与超时处理。
- 外部表来源变化的界面走查：M4 场景只在 API 层验证，没有在浏览器中走查。
- 其他数据库引擎：只有 StarRocks 和 MySQL 在真实环境验证过。规划中的引擎只做了解析层的单元测试（PostgreSQL 方言），不代表已支持。
- 元数据未刷新窗口内的**缓存**行为：M5 界面走查证实了这个缺口——删列后、采集前，同一问题会返回缓存的 300。已在 M5 用“执行前核对”修复，见 [acceptance-m5.md](acceptance-m5.md)。
- `httpx2` 2.16.0（2026-10-21 发布）仍不足两周，继续使用 httpx 0.28.1，留待 M5 复核。
