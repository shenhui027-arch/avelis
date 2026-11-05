# 开发指南

[English](development.en.md) | 简体中文 | [繁體中文](development.zh-TW.md)

本文面向在本机开发、调试与验收 Avelis 的开发者：后端与前端的开发命令、本地演示环境、一条命令复现验收。部署到服务器见 [deployment.md](deployment.md)，配置项见 [configuration.md](configuration.md)。

## 前置条件

- [uv](https://docs.astral.sh/uv/)（自动使用 Python 3.12）。Windows 上不要用裸 `python`，统一用 `uv run`。
- Node.js 24 与 pnpm 11。
- Docker Desktop / Docker Engine（Compose v2，为演示容器预留约 7 GB 内存）。

开发环境需要联网安装依赖（`uv sync`、`pnpm install`）；部署用的 Docker 镜像则在构建时装好全部依赖，运行时不联网下载，见 [deployment.md](deployment.md)。

开发时后端与前端分开运行：后端 `127.0.0.1:8000`，前端 Vite 开发服务器 `127.0.0.1:5173` 把 `/api` 代理到后端。生产环境由后端单端口同时提供接口与前端，见 [deployment.md](deployment.md)。

## 后端

```bash
cd backend
uv sync
uv run python -m avelis.bootstrap migrate      # 迁移元数据库（PostgreSQL）
uv run python -m avelis.bootstrap admin        # 创建自动化管理员 ops_admin 与本地超级管理员 admin（见下）
uv run uvicorn avelis.api.app:app --port 8000  # API；接口文档 http://127.0.0.1:8000/api/docs
uv run python -m avelis.jobs                   # 后台 worker：采集任务、定时轮询
uv run python -m avelis.config check           # 查看生效配置与来源（口令脱敏）
AVELIS_FRONTEND_DIR=frontend/dist uv run python -m avelis.server  # 按生产方式单端口运行（需先 pnpm build）
uv run pytest                                  # 单元测试
uvx ruff@0.17.3 check .                        # 代码检查（规则在 pyproject.toml；仓库根目录再跑一次检查脚本）
uv run pytest -m acceptance                    # 验收（会重置演示库与元数据库）
uv run python -m avelis.bootstrap demo --reset # 演示走查：重置并经 API 建好用户、业务域、数据源，完成首次采集
```

`bootstrap demo` 可用 `AVELIS_DEMO_SOURCE_HOST` 指定新建演示数据源的主机（默认 `127.0.0.1`）；在 Docker 部署容器里连接宿主演示库时设为 `host.docker.internal`。同名数据源已存在时直接复用，不修改其连接地址。

- **认证**：OAuth2 密码模式 + PyJWT 访问令牌（可在 Redis 吊销），口令用 pwdlib（Argon2）哈希；数据源口令用 Fernet 加密存储，接口永不回传。登录失败按“账号 + 来源地址”限流锁定（`AVELIS_LOGIN_MAX_FAILURES` / `AVELIS_LOGIN_LOCK_MINUTES`）；管理员创建或重置口令的账号首次登录必须修改；改口令、重置、停用后旧会话立即失效。
- **角色**：管理员、维护者、复核者、查询者，加上业务域成员（负责人 / 维护者 / 可查询）。默认禁止自审，策略见 `/api/settings`。
- **问数**：LangGraph 智能体，能多轮追问、结合上下文理解追问，会话状态存在 PostgreSQL。模型只提交结构化意图，SQL 由已发布口径确定性生成并校验。支持多个指标、排名与前 N、环比 / 同比，以及派生指标（如客单价 = 实付销售额 / 订单数），见 [ADR 0005](decisions/0005-ask-expressiveness.md)。未指定模型时用演示模型（规则匹配，答案中会标明）。
- **分析**：多步分析智能体，用于“为什么变了、什么关系、该怎么做”这类问题。
  - 取数只走受治理查询，计算由后端完成；
  - 报告中的数字逐个核验，结论分事实、计算、推断、建议；
  - 报告可导出 PDF / Markdown / CSV，分析可保存为定时任务。
  
  需要支持工具调用的真实模型，见 [ADR 0007](decisions/0007-analysis-agent.md)。演示用的合成电商数据集用 `uv run scripts/ec_data.py load` 灌入，`bootstrap demo` 会自动接入。
- **模型配置**：在“设置 / 模型配置”页面维护，支持 DeepSeek、OpenAI、Anthropic、通义千问、自定义 OpenAI 兼容接口。密钥加密存储，只显示末四位；检测会验证工具调用；按用途指定模型；默认向模型发送查询结果与脱敏后的代码片段（分析需要），可在同一页面关闭，需保密时接入本地模型。开发时也可用 `uv run python -m avelis.bootstrap model` 把 `.env` 的 `AVELIS_MODEL_*` 登记进去。
- **导入血缘**：上传 SQL 文件、粘贴 SQL、上传 Python / Shell 代码。解析出的边直接写入（可修改）；解析不了的片段由模型提出候选，必须人工接受、修改或驳回。
- **数据库引擎**：每种数据库是 `avelis.engines` 中的一个适配器（连接器、SQL 方言、资产名层级、能力声明），核心模块不按数据库类型分支，见 `GET /api/engines`。目前登记：StarRocks、PostgreSQL、ClickHouse、SQL Server、Oracle、Doris（采集、视图血缘、问数、执行前核对），以及 MySQL（采集，作为外部表的追溯目标）。更多引擎的演示库用 `uv run scripts/demo.py up --engines` 启动。新增数据库的步骤见 [ADR 0004](decisions/0004-engine-adapters.md)。
- **执行前核对**：每次问数执行前（含命中缓存）核对来源的列与视图定义是否与上次采集一致；不一致就拦截并排队采集（`AVELIS_ASK_LIVE_CHECK`，默认开启）。
- **字段含义**：以数据库列注释为准，Avelis 不另设显示名。问数与资产原样展示列名和注释，缺注释如实标出；数据源详情给出注释完整度与缺口清单（可导出），发布检查提示缺注释（`AVELIS_REQUIRE_COLUMN_COMMENTS=true` 时阻断），见 [ADR 0006](decisions/0006-column-meaning-from-db-comments.md)。
- **权限**：资产按业务域可见；未归属业务域的资产默认只对管理员与维护者可见（`AVELIS_UNASSIGNED_ASSET_POLICY=deny|allow`）。
- **worker**：`AVELIS_WORKER_SCHEDULE_SECONDS`（轮询，默认 60）、`AVELIS_WORKER_STALE_SECONDS`（未入队任务的扫描阈值，默认 30）、`AVELIS_WORKER_RUNNING_TIMEOUT_SECONDS`（运行超时记为失败，默认 3600）。
- 设计决定见 [ADR 0002](decisions/0002-backend-foundations.md)、[ADR 0003](decisions/0003-postgres-agent-model-settings.md)、[ADR 0004](decisions/0004-engine-adapters.md)。

单元测试不连接任何外部服务，可以离线运行（用一个假的密钥，不读 `.env`）：

```bash
cd backend
AVELIS_SECRET=offline-unit-test-secret-0123456789 AVELIS_ENV_FILE=/nonexistent uv run --frozen python -m pytest tests/unit -q -p no:cacheprovider
```

## 前端

账号：
- `admin`：本地超级管理员，拥有全部角色，密码是 `.env` 中的 `AVELIS_ADMIN_PASSWORD`。只在元数据库位于本机时创建，不经过密码策略校验；`bootstrap admin` 会把它重设为配置值。
- `ops_admin`：自动化管理员，演示准备与验收脚本用。
- 演示账号：zhang（交易域负责人）、zhao（商品域负责人）、li（复核者）、wang、sun（分析师）。

`ops_admin` 与演示账号的密码都是 `.env` 中的 `AVELIS_DEMO_USER_PASSWORD`。

```bash
cd frontend
pnpm install
pnpm dev            # http://127.0.0.1:5173，/api 代理到 127.0.0.1:8000
pnpm gen:api        # 后端接口变化后重新生成类型
pnpm gen:tokens     # 设计令牌变化后重新生成 tokens.css
```

样式全部来自 Avelis UI 设计系统：`frontend/design/tokens.json` 生成 `src/styles/tokens.css`，组件样式在 `src/styles/avelis-ui.css`，动效在 `src/styles/motion.css`，组件套件（图标块、标签、进度轨、提示条、空态等共用件）在 `src/styles/kit.css`，新组件只组合这些件，不另起配色。

### 视觉与文案

- 设计系统 v5（晴蓝，配色取自 21st.dev 主题 Twitter；以浅色为主，深色同等完成度）：白底、明亮蓝为品牌色，每个模块一种颜色（采集蓝、问数绿、血缘黄、目录紫、变更红），侧栏图标、统计卡、行首图标块与图表共用模块色（令牌 `--mod-*`，套件语气 `tone-collect` 等）。浅色主题按钮用较深的蓝 `#0a78cb` 保证文字对比，明亮蓝 `#1e9df1` 用于图标、条、指示与填充；按钮为胶囊形。字重 400–500（600 只用于 Logo），数字用等宽数字。字体 Geist + Noto Sans SC（随镜像打包，运行期不下载）。
- 不写口号、教学段落和区块下的说明文字，让布局、层级和主操作自己说明；确需补充时用 `Hint`（ⓘ + 提示）。表单标签与可访问名称保留；删除、提交永久变更等操作保留后果提示。
- 提示统一由全局提示层显示（`src/components/Tooltip.tsx`）：写 `data-tip` 或用 `Hint`；已有的 `title` 也会自动换成同样的提示。
- 动效只回应用户的操作（打开、展开、切换、确认），一条缓动曲线 `cubic-bezier(.22,1,.36,1)`：简单的进入与按压用 `motion.css`，侧边栏、分段控件、标签页的选中块用 Motion（`motion/react`）共享布局滑动；不放循环装饰动画；“减少动态效果”时全部关闭（`MotionConfig reducedMotion="user"`）。

### 界面控件

带弹出层的控件一律用 `src/components/controls.tsx` 里的组件（从 `components/ui` 导入），不用浏览器原生控件。原生弹出层由操作系统绘制，在深色界面里会变成白底或系统风格；问数输入框和变更记录都出过这个问题。

| 需要 | 用 | 不用 |
|---|---|---|
| 下拉选择 | `Select`（子元素照常写 `<option value>`，只当数据读取） | `<select>` |
| 日期 | `DateInput`（值是 `YYYY-MM-DD`） | `type="date"` 等日期时间输入 |
| 时间 | 两个 `Select`（时、分） | `type="time"` |
| 可自由输入、带候选 | `SuggestInput` | `<datalist>` / `list=` |
| 二次确认 | `ConfirmDialog` | `confirm()` / `alert()` / `prompt()` |
| 选项少、要一眼看全 | `Seg` / `Tabs` | — |

这些浮层挂到 `body` 并按触发器位置定位，面板的滚动和裁剪不会遮住它们；键盘可操作（方向键、Enter、Esc、首字母跳转）。自己写的弹出菜单若要判断“点到外面”，要把 `.av-float-root` 里的点击算作里面。

文本输入上自己处理方向键、Enter 等按键时，先跳过输入法组合态（`e.nativeEvent.isComposing` 或 `keyCode === 229`），否则中文输入法选词会被当成确认。

`pnpm check:ui` 检查源码里没有上表“不用”一列的写法，CI 里同样会跑。

## 演示环境

只用于本地开发与验收，所有端口只绑 `127.0.0.1`，数据全部是固定日期的合成电商数据：

- **StarRocks 3.5.14**（单容器）：交易事实表 `shop.orders`、`shop.order_items`、`shop.refund_raw`，视图 `shop.daily_sales`，分析视图 `ads.*`。
- **电商维表库 MySQL 8.4**：`shop_dim` 中的客户、商品、类目、区域。StarRocks 用两种方式访问：JDBC Catalog `shop_mysql`，以及旧式 `ENGINE=mysql` 外部表 `dim.ext_region`。
- **Avelis 元数据库**：PostgreSQL 18（也存放问数智能体的会话状态）。
- **Valkey 8**（Redis 协议）：任务队列、缓存、采集锁。

### 资源要求

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
5. 验收（故事 1–9 与异常场景，全部在真实 StarRocks / MySQL / PostgreSQL / Valkey 上执行）；
6. 前端类型检查与构建；
7. 把演示环境恢复到故事 1 的起点。

报告写入 `.demo/acceptance-report.md`。`.env` 配置了模型密钥时会运行 2 项真实模型用例，问题与指标元数据会发送到配置的模型服务。

## 设计

- 设计令牌：`frontend/design/tokens.json`，`pnpm gen:tokens` 生成 `frontend/src/styles/tokens.css`；组件样式在 `frontend/src/styles/avelis-ui.css`。
- 品牌标记：当前使用的标记在 `frontend/public/favicon.svg` 与 `frontend/src/components/Logo.tsx`。
- 文档写法见[文档规范](documentation-guide.md)。
