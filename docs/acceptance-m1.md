# M1 验收说明：后端闭环

- 日期：2026-10-29
- 环境：本机 Docker 演示环境（[ADR 0001](decisions/0001-demo-environment.md)），包括：
  - StarRocks 3.5.14 单节点；
  - 电商维表库 MySQL 8.4.10；
  - 元数据库 PostgreSQL 18 与 MySQL 8.4.10；
  - Valkey 8。
- 模型：演示适配器（规则匹配）。真实模型端到端**未验证**。

## 怎么复现

```bash
uv run scripts/demo.py up                          # 仓库根目录：起演示环境
cd backend
uv sync
uv run pytest                                      # 单元测试（不需要数据库）
AVELIS_META_BACKEND=pg    uv run pytest -m acceptance
AVELIS_META_BACKEND=mysql uv run pytest -m acceptance
```

验收测试开始时会做三件事：
- 把演示 StarRocks 重置到基线（`demo.py seed`）；
- 清空并迁移所选元数据库；
- 清除 Redis 中 `avelis:*` 的键。

之后所有操作都通过 HTTP API，以不同角色的真实账号执行：

| 账号 | 角色 |
|---|---|
| admin | 管理员（M1 时的约定；现在脚本使用 `ops_admin`，见 README） |
| zhang | 交易域负责人 |
| zhao | 商品域负责人 |
| li | 复核者 |
| wang | 分析师 |
| sun | 只属交易域 |
| qian | 无业务域 |

## 断言内容与实际结果

| 用例 | 对应故事 | 关键断言（检查实际执行行为，不只看状态码） |
|---|---|---|
| `test_00_setup` | 准备 | 管理员经 API 建用户、业务域、两个数据源、端点别名。接口不回传口令，返回体中也找不到口令原文 |
| `test_01_collect_and_lineage` | 1 | 详见下文 |
| `test_02_publish_paid_sales` | 2 | 负责人建“实付销售额”并提交。负责人自己批准被拒（403），复核者批准后发布。版本记录负责人、复核人、粒度、时区、允许维度与依赖指纹 |
| `test_03_ask_paid_sales` | 3 | 详见下文 |
| `test_04_05_definition_change_blocks_old_metric` | 4、5 | 详见下文 |
| `test_06a_restore_original_definition` | 6（恢复原定义） | 详见下文 |
| `test_06b_publish_net_sales` | 6（发布新口径） | 详见下文 |
| `test_09_grain_fan_out` | 9 | 详见下文 |
| `test_10_collect_via_job_queue` | 运行机制 | 不带 `wait` 的采集进入 job 表并经 Redis 唤醒；`run_job` 执行成功，结果指向新的采集批次。同一数据源采集互斥，锁被占用时返回 409 |

**test_01：采集与列级血缘**
- 真实采集了 12 个对象：StarRocks 内表、视图、旧式外部表，以及 Catalog 中的 4 张 MySQL 维表。
- `sales_amount ← orders.paid_amount` 记为“聚合取值”，`region` 记为“分组影响”，不存在 `refund_amount` 的边。
- 粒度：`orders` 由主键观察到 `order_id`；视图由 GROUP BY 推出 `[order_date, region]`。
- Catalog 表 `dim_product` 自动确认映射到 MySQL `shop_dim.dim_product`，并继承其主键作为粒度。
- 重复采集不产生新版本，也不产生变更集。

**test_03：问实付销售额**
- 分析师问“2026-09-15 华东区实付销售额”，后端真实执行，得到 **300.00**。
- 答案包含：SQL（带 LIMIT）、指标版本 V1、`sales_amount ← orders.paid_amount` 的血缘依据、数据新鲜度、演示模型标记。
- 第二次相同问题命中缓存。
- 无业务域的用户检索不到该口径，没有执行 SQL。

**test_04_05：定义变化后拦截旧口径**
- 用 root 把视图改为 `SUM(paid_amount - refund_amount)`，输出列名与类型不变，然后重新采集。
- 发现了 `definition_changed` 与 `derivation_changed(sales_amount)`，且没有出现字段级 schema 事件。
- 差异中可以看到新定义包含 `refund_amount`；记录了生效区间。
- 影响分析：指标 V1 受影响；下游视图 `ads.region_board` 列入影响范围；生成复核任务，指派给负责人。
- 再次问“实付销售额”：返回 `blocked`，**`sql` 为空、`executed_at` 为空、`result` 为空**，即没有执行，不会把 270 当成实付销售额返回，也没有切换口径。
- 历史答案保留 300，并标记“需重新确认”。审计中有系统记录的 `metric_version.affected`。

**test_06a：恢复原定义**
- 定义仍是净额时，按“已恢复”结案被拒（409）。
- 恢复视图并重新采集后，指纹回到原值，但在复核结案前问数仍被拦截。
- 非负责人处理复核任务被拒（403）。
- 负责人结案后指标重新发布，再问得到 **300.00**。

**test_06b：发布退款后净销售额**
- 再次改为净额，产生新的复核任务。
- “退款后净销售额”发布前问它，得到“请澄清”。
- 负责人建立该指标，复核者批准。
- 结案时必须指定一个已发布的新版本。
- 之后问净销售额得到 **270.00**，并显示新口径与版本。
- 原指标转为 `retired`，状态历史完整：草稿 → 待复核 → 已发布 → 受影响 → 已发布 → 受影响 → 失效。
- 再问原指标被拦截，并提示改用新口径；历史答案仍保留原版本与 300；变更集自动结案。

**test_09：粒度与关联放大**
- 关联基数推导：订单→明细 1:N，订单→退款 1:N，明细→订单 N:1，明细→商品 N:1（右侧粒度来自 MySQL 主键），商品→类目 N:1。
- 跨域路径需要两个域都批准：只有一方批准时仍为待批；非负责人不能代表另一个域。
- 退款表粒度 `refund_id` 由人工声明，经只读查询核验通过；把 `order_id` 声明为粒度时核验失败，并给出重复样例 1001。
- “订单金额 × 1:N 明细”的指标提交被拒，原因含“1:N … 重复计数”；StarRocks 对这种写法实际返回 400。
- 改为先聚合再关联后通过，再问得到 **300.00**。
- 跨域“类目销售额”在商品域批准前提交被拒；批准后三步关联都是 N:1，再问“厨具”得到 **270.00**，依据追到 MySQL 真实表 `def.shop_dim.dim_product`。
- 只属交易域的用户问同一问题，被拒绝执行，原因是来源含商品域资产。

最近一次实际运行：

```
$ uv run pytest
29 passed, 9 deselected
$ AVELIS_META_BACKEND=pg uv run pytest -m acceptance
9 passed, 29 deselected in 16.96s
$ AVELIS_META_BACKEND=mysql uv run pytest -m acceptance
9 passed, 29 deselected in 19.51s
```

## 尚未覆盖（在后续里程碑）

- **故事 7**：删除被依赖字段，受影响查询受限，同时无关指标照常可查。对应的指纹 `broken` 逻辑已实现，但还没有验收用例（M4）。
- **其他异常场景**：同名对象歧义、同名重建、元数据过期、查询超时的端到端、注入、外部表列不一致、MySQL 侧改字段经身份映射传导（M4）。
- **独立 worker 进程**：`python -m avelis.jobs` 的主循环（定时入队、扫描积压）没有作为独立进程运行验证；单个任务的执行已经验证。
- **真实模型**：意图解析的真实模型端到端（需要 key，M2）。
