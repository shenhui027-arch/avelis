# Avelis

有依据的智能问数：建立在数据库血缘、业务口径和变更管理之上。每个答案都带来源、口径、版本和权限依据；上游变化后，系统知道哪些答案需要重新确认。首期面向 StarRocks。

> 当前阶段：**v0.1 里程碑 M0–M5 已完成**：后端闭环、LangGraph 问数智能体、模型配置、导入血缘、工作台界面、异常与加固、交付。验收汇总见 [docs/acceptance.md](docs/acceptance.md)（一条命令从零复现），各里程碑见 [M1](docs/acceptance-m1.md)–[M5](docs/acceptance-m5.md)。架构与治理规则见 [docs/architecture.md](docs/architecture.md)；范围、设计决定和里程碑见 [docs/PLAN.md](docs/PLAN.md)，StarRocks 实机核验结论见 [docs/starrocks-notes.md](docs/starrocks-notes.md)。

## 一条命令复现验收

前置条件：Docker（Compose v2，为容器预留约 7 GB 内存）、[uv](https://docs.astral.sh/uv/)、pnpm。

```bash
uv run scripts/acceptance.py --fresh     # 从零：删除演示容器与数据卷后重建，再跑全部验收
uv run scripts/acceptance.py             # 复用已启动的演示容器
uv run scripts/acceptance.py --no-llm    # 不调用真实模型（不向模型服务发送任何数据）
```

依次执行：

1. 生成 `.env`（口令随机生成、不打印）；
2. 校验 JDBC 驱动，启动容器，灌入基线并核对 8 项基线数值；
3. 迁移元数据库；
4. 后端单元测试；
5. 验收（故事 1–9 与 M4 异常场景，全部在真实 StarRocks / MySQL / PostgreSQL / Valkey 上执行）；
6. 前端类型检查与构建；
7. 把演示环境恢复到故事 1 的起点。

报告写入 `.demo/acceptance-report.md`。`.env` 配置了模型密钥时会运行 2 项真实模型用例，问题与指标元数据会发送到配置的模型服务。各项预期值与实际结果见 [docs/acceptance.md](docs/acceptance.md)。

## 仓库结构

```
backend/              后端（FastAPI）：采集、血缘、粒度、口径治理、变更复核、问数智能体、导入血缘、模型配置
frontend/             前端（React + Vite）：工作台、血缘、导入、目录、变更、问数、设置
deploy/demo/          演示环境：Compose、StarRocks 启动包装、电商维表库初始化、合成数据、驱动锁定
scripts/demo.py       演示环境起停、灌数、基线核对、实机核验
scripts/sr_probe.py   StarRocks 实机核验项
docs/                 计划、核验记录、架构决定
```

产品采用前后端分离：后端 `backend/`（FastAPI，REST + OpenAPI），前端 `frontend/`（React SPA，只通过 `/api` 访问后端，类型由 OpenAPI 生成）。元数据库只支持 PostgreSQL（[ADR 0003](docs/decisions/0003-postgres-agent-model-settings.md)）。

## 后端

```bash
cd backend
uv sync
uv run python -m avelis.bootstrap migrate      # 迁移元数据库（PostgreSQL）
uv run python -m avelis.bootstrap admin        # 创建管理员 admin（口令见 .env 的 AVELIS_DEMO_USER_PASSWORD）
uv run uvicorn avelis.api.app:app --port 8000  # API；接口文档 http://127.0.0.1:8000/api/docs
uv run python -m avelis.jobs                   # 后台 worker：采集任务、定时轮询
uv run pytest                                  # 单元测试
uv run pytest -m acceptance                    # 验收（会重置演示库与元数据库）
uv run python -m avelis.bootstrap demo --reset # 演示走查：重置并经 API 建好用户、业务域、数据源，完成首次采集
```

- **认证**：OAuth2 密码模式 + PyJWT 访问令牌（可在 Redis 吊销），口令用 pwdlib（Argon2）哈希；数据源口令用 Fernet 加密存储，接口永不回传。登录失败按“账号 + 来源地址”限流锁定（`AVELIS_LOGIN_MAX_FAILURES` / `AVELIS_LOGIN_LOCK_MINUTES`）；管理员创建或重置口令的账号首次登录必须修改；改口令、重置、停用后旧会话立即失效。
- **角色**：管理员、维护者、复核者、查询者，加上业务域成员（负责人 / 维护者 / 可查询）。默认禁止自审，策略见 `/api/settings`。
- **问数**：LangGraph 智能体，能多轮追问、结合上下文理解追问，会话状态存在 PostgreSQL。模型只提交结构化意图，SQL 由已发布口径确定性生成并校验。支持多个指标、排名与前 N、环比 / 同比，以及派生指标（如客单价 = 实付销售额 / 订单数），见 [ADR 0005](docs/decisions/0005-ask-expressiveness.md)。未指定模型时用演示模型（规则匹配，答案中会标明）。
- **模型配置**：在“设置 / 模型配置”页面维护，支持 DeepSeek、OpenAI、Anthropic、通义千问、自定义 OpenAI 兼容接口。密钥加密存储，只显示末四位；检测会验证工具调用；按用途指定模型；数据外发默认关闭。开发时也可用 `uv run python -m avelis.bootstrap model` 把 `.env` 的 `AVELIS_MODEL_*` 登记进去。
- **导入血缘**：上传 SQL 文件、粘贴 SQL、上传 Python / Shell 代码。解析出的边直接写入（可修改）；解析不了的片段由模型提出候选，必须人工接受、修改或驳回。
- **数据库引擎**：每种数据库是 `avelis.engines` 中的一个适配器（连接器、SQL 方言、资产名层级、能力声明），核心模块不按数据库类型分支，见 `GET /api/engines`。目前登记 StarRocks（采集、视图血缘、问数）、PostgreSQL（采集、视图血缘、问数）与 MySQL（采集，作为外部表的追溯目标）。新增数据库的步骤见 [ADR 0004](docs/decisions/0004-engine-adapters.md)。
- **执行前核对**：每次问数执行前（含命中缓存）核对来源的列与视图定义是否与上次采集一致；不一致就拦截并排队采集（`AVELIS_ASK_LIVE_CHECK`，默认开启）。
- **权限**：资产按业务域可见；未归属业务域的资产默认只对管理员与维护者可见（`AVELIS_UNASSIGNED_ASSET_POLICY=deny|allow`）。
- **worker**：`AVELIS_WORKER_SCHEDULE_SECONDS`（轮询，默认 60）、`AVELIS_WORKER_STALE_SECONDS`（未入队任务的扫描阈值，默认 30）、`AVELIS_WORKER_RUNNING_TIMEOUT_SECONDS`（运行超时记为失败，默认 3600）。
- 设计决定见 [ADR 0002](docs/decisions/0002-backend-foundations.md)、[ADR 0003](docs/decisions/0003-postgres-agent-model-settings.md)、[ADR 0004](docs/decisions/0004-engine-adapters.md)。

## 前端

演示账号：admin、zhang（交易域负责人）、zhao（商品域负责人）、li（复核者）、wang、sun（分析师），口令都是 `.env` 中的 `AVELIS_DEMO_USER_PASSWORD`。

```bash
cd frontend
pnpm install
pnpm dev            # http://127.0.0.1:5173，/api 代理到 127.0.0.1:8000
pnpm gen:api        # 后端接口变化后重新生成类型
pnpm gen:tokens     # 设计令牌变化后重新生成 tokens.css
```

样式全部来自 Avelis UI 设计系统：`design/tokens.json` 生成 `src/styles/tokens.css`，组件样式在 `src/styles/avelis-ui.css`。

## 演示环境

只用于本地开发与验收，所有端口只绑 `127.0.0.1`，数据全部是固定日期的合成电商数据：

- **StarRocks 3.5.14**（单容器）：交易事实表 `shop.orders`、`shop.order_items`、`shop.refund_raw`，视图 `shop.daily_sales`，分析视图 `ads.*`。
- **电商维表库 MySQL 8.4**：`shop_dim` 中的客户、商品、类目、区域。StarRocks 用两种方式访问：JDBC Catalog `shop_mysql`，以及旧式 `ENGINE=mysql` 外部表 `dim.ext_region`。
- **Avelis 元数据库**：PostgreSQL 18（也存放问数智能体的会话状态）。
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

worker 长时间运行验证（会临时把采集间隔改为 1 分钟，期间重启 Valkey、修改一次视图，结束后恢复）：

```bash
cd backend && uv run python ../scripts/worker_soak.py --minutes 60
```

### 端口与账号

| 服务 | 地址 | 账号 |
|---|---|---|
| StarRocks（MySQL 协议） | `127.0.0.1:19030` | `root`（只用于灌数）、`avelis_collector`（采集）、`avelis_reader`（只读查询） |
| StarRocks FE HTTP | `127.0.0.1:18030` | — |
| 电商维表库 MySQL | `127.0.0.1:13306`（容器网络内 `shop-mysql:3306`） | `avelis_collector`（只读元数据）、`sr_catalog`（JDBC Catalog）、`sr_legacy`（旧式外部表） |
| 元数据库 PostgreSQL | `127.0.0.1:15432` | `avelis` |
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
