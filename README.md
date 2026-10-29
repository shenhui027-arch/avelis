# Avelis

有依据的智能问数：建立在数据库血缘、业务口径和变更管理之上。每个答案都带来源、口径、版本和权限依据；上游变化后，系统知道哪些答案需要重新确认。首期面向 StarRocks。

> 当前阶段：v0.1 开发中，**M0 基线与实机核验已完成**。范围、设计决定和里程碑见 [docs/PLAN.md](docs/PLAN.md)，StarRocks 实机核验结论见 [docs/starrocks-notes.md](docs/starrocks-notes.md)。

## 仓库结构

```
deploy/demo/          演示环境：Compose、StarRocks 启动包装、电商维表库初始化、合成数据、驱动锁定
scripts/demo.py       演示环境起停、灌数、基线核对、实机核验
scripts/sr_probe.py   StarRocks 实机核验项
docs/                 计划、核验记录、架构决定
```

产品采用前后端分离：后端 `backend/`（FastAPI）从 M1 开始，前端 `frontend/`（React）从 M3 开始。

## 演示环境

只用于本地开发与验收，所有端口只绑 `127.0.0.1`，数据全部是固定日期的合成电商数据：

- **StarRocks 3.5.14**（单容器）：交易事实表 `shop.orders`、`shop.order_items`、`shop.refund_raw`，视图 `shop.daily_sales`，分析视图 `ads.*`。
- **电商维表库 MySQL 8.4**：`shop_dim` 中的客户、商品、类目、区域。StarRocks 用两种方式访问：JDBC Catalog `shop_mysql`，以及旧式 `ENGINE=mysql` 外部表 `dim.ext_region`。
- **Avelis 元数据库**：PostgreSQL 18 与 MySQL 8.4 各一个，产品两者都兼容。
- **Valkey 8**（Redis 协议）：任务队列、缓存、采集锁。

### 前置条件

- Docker Desktop（为容器预留约 7 GB 内存；StarRocks 容器上限 5.25 GB）
- [uv](https://docs.astral.sh/uv/)（自动使用 Python 3.12）

### 常用命令

```bash
uv sync
uv run scripts/demo.py up        # 生成 .env、校验 JDBC 驱动、启动、等待就绪、灌数并核对基线
uv run scripts/demo.py check     # 再次核对验收基线
uv run scripts/demo.py seed      # 把演示库重置到基线
uv run scripts/demo.py verify    # StarRocks 实机核验，结果写入 .demo/sr-verify.json
uv run scripts/demo.py down      # 停止；加 --volumes 同时删除数据
```

### 端口与账号

| 服务 | 地址 | 账号 |
|---|---|---|
| StarRocks（MySQL 协议） | `127.0.0.1:19030` | `root`（只用于灌数）、`avelis_collector`（采集）、`avelis_reader`（只读查询） |
| StarRocks FE HTTP | `127.0.0.1:18030` | — |
| 电商维表库 MySQL | `127.0.0.1:13306`（容器网络内 `shop-mysql:3306`） | `avelis_collector`（只读元数据）、`sr_catalog`（JDBC Catalog）、`sr_legacy`（旧式外部表） |
| 元数据库 PostgreSQL | `127.0.0.1:15432` | `avelis` |
| 元数据库 MySQL | `127.0.0.1:13307` | `avelis` |
| Valkey | `127.0.0.1:16379` | 口令认证 |

所有口令由 `demo.py init-env` 随机生成并写入 `.env`（不进仓库，也不会打印）。演示网络使用固定子网 `10.240.17.0/24`，与本机网络冲突时在 `.env` 修改 `AVELIS_DEMO_SUBNET`。

### 验收基线（`demo.py check`，用只读账号查询）

| 项 | 期望 |
|---|---|
| `shop.daily_sales` 2026-09-15 华东（实付） | 300.00 |
| 同日同区退款后净额 | 270.00 |
| 订单直接关联退款明细后求和（粒度放大） | 400.00 |
| 订单直接关联订单明细后求和（粒度放大） | 400.00 |
| `ads.sales_by_category` 2026-09-15 厨具（关联 MySQL 维表） | 270.00 |
| `ads.member_sales` 2026-09-15 gold（关联 MySQL 维表） | 250.00 |
| `ads.region_board` 华东 2026-09-15（经旧式外部表） | 300.00 |
| Catalog 中商品维表行数 | 4 |

## 设计

- 设计系统 Avelis UI：<https://claude.ai/artifact/A64c2HEAdLnzYuRbpe49BB>
- 交互原型：<https://claude.ai/artifact/REhQcCU22GwMcFGen5prw3>（原型里的示例数据仍是早期的半导体场景，后续会换成本仓库的电商数据）
