# 更新日志

本文件记录 Avelis 各版本的主要变化，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [1.0.4] - 2026-11-04

用对话改图，图上标注与高亮；Webhook 并发投递，发件箱按保留期清理；资产选择与目录检索按需查询。以下新功能从 v1.0.4 开始提供，v1.0.3 及更早版本不包含。

### 新增

- 用对话改图（ADR 0020 第 4 阶段）：续问只要求改呈现时（“画成饼图”“按从低到高排”“只看前 5”“标出最高点和平均线”“突出华东”“只画销售额”），沿用上一轮的数据、只让模型调用一次 `chart` 工具，不执行任何查询；有多张图时按用户说法选，选不出就追问；做不到时说明可选项，不硬画。需要新数据的续问照常取数（[#110](https://github.com/shenhui027-arch/avelis/pull/110)）。
- `chart` 工具新增参数 `sort`（按数值升序 / 降序或按类目 / 时间顺序）、`marks`（标出最高 / 最低 / 平均）、`highlight`（突出最多 5 个类目）与 `measures`（指定画哪些度量及顺序，可用列名或显示名）。图表偏好只有一份契约 `ChartPreference`，分析与问数的 chart 工具、选图器校验与报告插图都从它派生（[#106](https://github.com/shenhui027-arch/avelis/pull/106)、[#110](https://github.com/shenhui027-arch/avelis/pull/110)）。
- 图表规格新增 `marks` 与 `highlight`：页面画出极值点与平均线、弱化其余类目；环形图与堆叠图上画不出的标注在图下用文字给出；PDF / HTML 导出同样绘制，Markdown 在图下列出（[#110](https://github.com/shenhui027-arch/avelis/pull/110)）。
- 资产选择器按服务端搜索：关联路径、指标基础资产、变更记录筛选输入即搜索，目录页与业务域详情的资产列表按页查询，不再整表加载；`GET /api/assets` 新增 `q`、`domain_id`、`ids`、`queryable`、`limit`、`offset`，新增 `GET /api/catalog/counts`（[#109](https://github.com/shenhui027-arch/avelis/pull/109)、[#111](https://github.com/shenhui027-arch/avelis/pull/111)、[#113](https://github.com/shenhui027-arch/avelis/pull/113)）。
- 发件箱保留期：已投递的通知快照默认保留 30 天（`[events] delivered_retention_days`），死信默认保留 90 天（`[events] dead_retention_days`，迁移 0024 新增进入死信的时间），worker 分批清理；待投递的不清理（[#107](https://github.com/shenhui027-arch/avelis/pull/107)、[#111](https://github.com/shenhui027-arch/avelis/pull/111)）。

### 变更

- Webhook 改为并发投递：`[webhook] concurrency` 默认 4（1 为串行），同一订阅同一时刻最多一个请求在途，每个请求的期限覆盖 DNS 与 HTTP；慢订阅不再拖住其他订阅，投递与业务任务分开调度（[#107](https://github.com/shenhui027-arch/avelis/pull/107)、[#111](https://github.com/shenhui027-arch/avelis/pull/111)）。
- 带维度的多指标对比图画全部指标：每个指标一个面板，超过 8 个面板在图下注明（[#106](https://github.com/shenhui027-arch/avelis/pull/106)）。
- 分析的描述统计与分组统计遵守可加性：比率、均值等非可加列不再给出合计，工具返回里说明原因；同一次查询带齐分子分母时重算整体比率（[#106](https://github.com/shenhui027-arch/avelis/pull/106)）。
- 导出里的“个百分点”“万 / 亿”等系统单位按导出语言显示（[#106](https://github.com/shenhui027-arch/avelis/pull/106)）。
- 目录、MCP 检索与列表接口的权限、关键词、业务域过滤和分页在数据库里执行，消除逐个口径查询；资产详情只读本资产的字段指纹（[#109](https://github.com/shenhui027-arch/avelis/pull/109)）。
- Ossie 导入时每个版本只保留自身引用的内容，根扩展每批只存一份原文（导出逐字还原），并有 20 MiB 总保留上限（[#109](https://github.com/shenhui027-arch/avelis/pull/109)）。
- 数据集翻页时页码与显示的数据一致，新页到达前暂停翻页；全屏下载 CSV 显示进度与失败原因（[#108](https://github.com/shenhui027-arch/avelis/pull/108)）。
- 迁移 0023、0024：发件箱清理用的部分索引与死信时间。

### 修复

- 堆叠柱图的极值标注指向本段末端，不再落进其他系列；堆叠图的平均值改为图下文字（[#112](https://github.com/shenhui027-arch/avelis/issues/112)，[#110](https://github.com/shenhui027-arch/avelis/pull/110)）。

### 已知局限

- 业务域列表仍整表加载（规模随组织结构，接口已在数据库侧过滤）。
- Webhook 默认并发 4、整轮预算 5 秒：前 4 个请求都占满期限时，后面的订阅要到下一轮才开始；每个在途请求占用一个元数据库连接，调大并发时需同步调大 `[database] pool_size`。
- 对话改图只改上一轮的图，不跨多轮回溯；页面用 ECharts、导出用 reportlab，线宽、字号等细节不完全相同。

## [1.0.3] - 2026-11-04

图表能力升级：问数回答出图，分析报告正文插图，导出按同一份图表规格绘图，数值按量纲显示。以下新功能从 v1.0.3 开始提供，v1.0.2 及更早版本不包含。

### 新增

- 问数回答出图（ADR 0020）：趋势为折线（长序列可缩放），排名与构成为排好序的条形（可切环形），对比为变化发散条或分组柱，单行为指标卡，不同量纲的多个度量为分图；图、候选图、表格一键切换，可下载 PNG；深浅主题自动切换。图表库为 ECharts 6.1.0，按需引入并单独分块，首次出图时才加载。
- 问数的解释模型可调用 `chart` 工具按需选图并给出一句结论式图题；只能从当前数据合法的图里选，由后端选图器校验；没有模型或模型不支持工具调用时沿用规则默认图。
- 分析报告正文插图：分析智能体可用 `chart` 工具把关键数据集作为图放进报告（每份最多 4 张，结论式图题，图类型由选图器校验）；没选图时按被引用最多的关键数据集自动配图。证据栏与数据集视图用同一套图表组件。
- 分析报告导出按同一份图表规格绘图：PDF / HTML 支持折线、条形、分组、堆叠、变化发散、环形、分图与指标，配色与界面一致；Markdown 导出对单系列的折线与条形输出 Mermaid `xychart-beta`，多系列与其他图保留表格。
- 问数答案与分析数据集返回图表规格 `charts`：后端按列契约（角色、量纲、期望方向）确定性选图，给出默认图与可切换的候选，数据已排序、截断并合并“其他”，页面与导出共用一份。

### 变更

- 问数结果表、取数类回答与分析数据集表格按量纲显示数值（与报告正文一致：比率显示为百分比，从 1 万起按万 / 亿或 K / M / B 缩写，带口径单位，差额与变化率带符号）；表头使用显示名（时间列按粒度显示为“日期 / 周 / 月份 / 季度 / 年”）；问数结果表可点表头排序。
- 问数的模型解释改为面向业务读者的一到三句要点：只发送指标显示名和按量纲格式化的值（超过 20 行发摘要），不再出现口径编码、版本、指纹和整张表；解释显示为正文。回答模板句里的数值同样按量纲显示。
- 同名口径（不同业务域）的追问选项显示为“名称（业务域）”，选择后可唯一定位。
- 问数失败时记录完整堆栈；连不上数据源时提示“查询数据源失败”，不再笼统显示“模型调用失败”。
- 设计令牌新增图表分类色 `viz-1`…`viz-8` 与中性色、网格、坐标轴色，深浅主题分别校验过色盲区分度。

### 修复

- 多指标合查按具体版本执行：两个派生口径锁定同一基础口径的不同版本时，原来合查会互相覆盖、绕过旧版本拦截；现在各自按锁定版本取值，单查与合查一致（[#102](https://github.com/shenhui027-arch/avelis/pull/102)）。
- 工作台“最近变更”按业务域过滤，不再显示无权访问的资产名称与变更活动（[#102](https://github.com/shenhui027-arch/avelis/pull/102)）。
- 分析的描述统计保留精确数字：合计、均值、极值不再先转成浮点数，大整数与极小的比率不再算错或被舍成 0（[#102](https://github.com/shenhui027-arch/avelis/pull/102)）。
- 合并后的问数入口显示“依据已受影响”：所用口径因上游变化待复核时，取数答案标出并说明数值按当时口径计算（[#102](https://github.com/shenhui027-arch/avelis/pull/102)）。
- 图表省略类目时如实提示：超过展示上限或按前 N 显示时，页面与 PDF / HTML / Markdown 导出都写明省略了几个类目及完整数据在哪里（[#103](https://github.com/shenhui027-arch/avelis/pull/103)）。
- Ossie 导入与导出自检在一次操作内只读一次字段指纹，不再随指标数成倍查询全目录（[#103](https://github.com/shenhui027-arch/avelis/pull/103)）。
- MCP 的匿名 OAuth 入口加资源上限：动态注册与授权请求限制正文与字段大小，按来源限流（注册 20 次 / 小时、授权 120 次 / 小时），未授权的动态客户端总量上限 1000；worker 定时清理过期票据、授权码与令牌（已用授权码在令牌族存续期间保留，用于识别重放）；“MCP 接入”的客户端列表分页。新增 `[mcp]` 下 6 个配置项，见配置参考（[#104](https://github.com/shenhui027-arch/avelis/pull/104)）。

### 已知局限

- 用对话改图（“画成饼图”“标出最高点”）与图上标注尚未提供（ADR 0020 第 4 阶段）。
- 页面用 ECharts、导出用 reportlab，图的类型与配色一致，线宽、字号等细节不完全相同；“原因排序”贡献图沿用原画法。
- 导出中“个百分点”等单位文案在英文与繁体中文下仍显示简体中文。
- 同时按时间和维度看多个指标时，分图最多 8 个面板，超出部分在图下注明并可在表格中查看。

## [1.0.2] - 2026-11-03

新增 MCP 服务、治理事件与 Webhook、口径与 Apache Ossie 互通、口径语义提示四项功能，部署改为开箱即用。以下新功能都从 v1.0.2 开始提供，v1.0.1 及更早版本不包含。

### 新增

- MCP 服务（ADR 0019，[#83](https://github.com/shenhui027-arch/avelis/pull/83)）：外部 AI 工具（Claude、Cursor 等）可通过 `/mcp` 使用受治理的口径与血缘，工具为 `search_metrics`、`get_metric`、`ask_metric`（结构化问数，不接受 SQL）、`lineage_neighbors`、`list_changes`。鉴权为 Avelis 内置 OAuth 2.1（授权码 + PKCE S256、资源绑定、可吊销，支持动态注册与客户端元数据文档），首次连接在 Avelis 里登录并确认授权；“设置 → MCP 接入”可查看连接地址、撤销授权，管理员可禁用客户端。每次调用记审计。默认开启；未配置对外地址时按访问地址自动推导（[#89](https://github.com/shenhui027-arch/avelis/pull/89)）。
- 治理事件与 Webhook（ADR 0018，[#82](https://github.com/shenhui027-arch/avelis/pull/82)、[#86](https://github.com/shenhui027-arch/avelis/pull/86)）：14 种治理事件（口径提交 / 发布 / 驳回 / 受影响 / 失效、关联路径批准 / 受影响、采集完成 / 失败 / 结构变化、血缘导入完成 / 失败、变更归因不可用、登录失败超阈值）写入审计并推送到订阅地址；失败事件与失败状态在同一事务写入，同一次执行只记一条；发件箱至少一次投递、退避重试、死信可重投；HMAC-SHA256 签名，密钥只显示一次。“设置 → 通知订阅”管理，事件表见 docs/events.md。
- 口径与 Apache Ossie 互通（ADR 0017，[#85](https://github.com/shenhui027-arch/avelis/pull/85)）：按业务域导出已发布口径为 Ossie 语义模型（锁定规范提交 `48da55a`，`0.2.0.dev0`，导出按官方 schema 校验，并先用导入校验自检，导不回来就不导出）；导入做结构、模型内部、对照已采集资产三层校验，任一层失败整体不写入，通过后只生成草稿、走复核发布；兼容旧版（`88e0011`）的 `semantic_model` 包装格式。业务域详情页“导出 Ossie”，口径目录“导入 Ossie”。
- 口径语义提示 `ai_context`（ADR 0017 §3，[#81](https://github.com/shenhui027-arch/avelis/pull/81)）：口径详情页“模型提示”可写适用说明、同义词、示例问法，问数与分析检索时一起匹配，拿不准时按适用说明澄清；修改不生成新版本、写审计，不进入 SQL 与依赖指纹。
- 登录页可直接修改密码：填账号、当前密码与新密码，改完回到原先要去的页面（[#90](https://github.com/shenhui027-arch/avelis/pull/90)）。

### 变更

- 部署开箱即用（[#89](https://github.com/shenhui027-arch/avelis/pull/89)）：
  - 未配置初始口令时，首次启动创建管理员 `admin` / `123456`，不强制改密；配置了初始口令则仍须满足强度要求、首次登录强制改密。上线后请立即修改默认口令，见 SECURITY.md。
  - 自动迁移、自动创建管理员、MCP 服务、分析的元数据图证据发现与证据解释默认开启，均可在配置里关闭。
- Docker Compose 默认只监听 `127.0.0.1`；需要从其他机器访问时，先修改管理员口令，再把 `AVELIS_BIND_ADDRESS` 设为对外地址（[#90](https://github.com/shenhui027-arch/avelis/pull/90)）。
- 前端统一使用自有表单控件，不再出现浏览器原生下拉、日期选择与确认框，CI 检查（[#79](https://github.com/shenhui027-arch/avelis/pull/79)）。
- 变更页按数据源标识变更集，首次采集单独说明（[#78](https://github.com/shenhui027-arch/avelis/pull/78)）。
- 演示脚本可用 `AVELIS_DEMO_SOURCE_HOST` 指定演示库主机，便于在 Docker 部署里灌入演示数据（[#90](https://github.com/shenhui027-arch/avelis/pull/90)）。

### 修复

- 代码辅助的凭据脱敏改为按值的语法单元读取，并在其上加整行兜底，修复 v1.0.1 已知局限中的四种漏遮写法（[#76](https://github.com/shenhui027-arch/avelis/pull/76)、[#77](https://github.com/shenhui027-arch/avelis/pull/77)，[#70](https://github.com/shenhui027-arch/avelis/issues/70)）。
- 联想输入框在输入法组合时不再截获方向键与回车（[#80](https://github.com/shenhui027-arch/avelis/issues/80)）。
- Ossie 派生口径导出按已校验的 Python AST 构造核心表达式，保留关键字口径编码、连续减号与括号的计算语义；组成口径的同名维度映射到不同列时拒绝内联，避免改变分组结果（[#88](https://github.com/shenhui027-arch/avelis/issues/88)）。

### 已知局限

- Ossie 导出：Ossie 核心模型表达不了的语义（预聚合关联、仅用于过滤行的关联、带分组的固定过滤、跨时间期末值 / 平均值等）会拒绝导出该口径并说明原因（ADR 0017 第 6 节）。
- 默认管理员口令 `123456` 是公开已知的，改密前不要把服务开放到不受信任的网络。

## [1.0.1] - 2026-11-02

补齐 MySQL 引擎，四个数据库接入 DDL 变更归因，并修复 1.0.0 之后发现的问题。

### 新增

- MySQL 完整引擎：视图列级血缘、只读问数（只读事务、服务端超时与行数上限）、执行前核对（[#66](https://github.com/shenhui027-arch/avelis/pull/66)）。
- 四个数据库的 DDL 变更归因（[#66](https://github.com/shenhui027-arch/avelis/pull/66)、[#72](https://github.com/shenhui027-arch/avelis/pull/72)）：
  - MySQL：可选绑定审计表（如 `mysql.general_log`），日志只作候选证据，与采集到的结构变化交叉核对；
  - SQL Server：读取默认跟踪，只采用已提交的对象事件；
  - Oracle：读取统一审计，只采用执行成功的记录；
  - StarRocks：可选绑定 AuditLoader 审计表，只采用执行成功的记录。
  未绑定审计表时视为没有审计来源；未开启、无权限、结构不符分别如实提示。四库均在真实实例上验证。
- 批量导入的执行函数规则支持原子追加（`POST /api/imports/sql-functions/{datasource_id}`），“加入规则”不再覆盖已有规则（[#64](https://github.com/shenhui027-arch/avelis/pull/64)，[#51](https://github.com/shenhui027-arch/avelis/issues/51)）。

### 修复

- 代码辅助的凭据脱敏改为按逻辑行扫描：覆盖 `--password` / `--api-key` 等 CLI 参数、带前缀的键、跨行引号值与空白行后的值，病态输入保持线性耗时（[#64](https://github.com/shenhui027-arch/avelis/pull/64)、[#71](https://github.com/shenhui027-arch/avelis/pull/71)，[#49](https://github.com/shenhui027-arch/avelis/issues/49)）。
- 批量导入的后台阶段与人工复核按同一顺序加锁，消除死锁风险（[#52](https://github.com/shenhui027-arch/avelis/issues/52)）。
- Oracle 上按期末值 / 平均值合成的时间桶内部列名不合法（[#55](https://github.com/shenhui027-arch/avelis/issues/55)）。
- DDL 归因：SQL Server 按服务器 collation 比较库名与模式名（[#67](https://github.com/shenhui027-arch/avelis/issues/67)）；StarRocks `SWAP WITH` 两端都保留候选（[#68](https://github.com/shenhui027-arch/avelis/issues/68)）、排除临时表、识别外部 Catalog；MySQL 列名与库表名按服务器规则比较大小写；SQL Server 默认跟踪异步写盘，读取前最多等待 10 秒（[#72](https://github.com/shenhui027-arch/avelis/pull/72)）。

### 工程

- 前端 `*.test.mjs` 纳入 CI（[#40](https://github.com/shenhui027-arch/avelis/issues/40)）；修复在不同 Python 版本上结果不稳定的测试（[#53](https://github.com/shenhui027-arch/avelis/issues/53)）。
- 全量 `ruff format`，取消超长行豁免，CI 增加格式检查（[#69](https://github.com/shenhui027-arch/avelis/pull/69)，[#39](https://github.com/shenhui027-arch/avelis/issues/39)）。

### 已知局限

- 凭据脱敏仍漏遮四种写法：Python 括号换行、字符串隐式拼接、YAML 折行值、SQL `IDENTIFIED BY`（[#70](https://github.com/shenhui027-arch/avelis/issues/70)）。
- 归因中尚未实机验证的情况：SQL Server 与 StarRocks 非 UTC 时区的时间换算、Oracle 无审计记录且无策略时的提示、StarRocks 外部 Catalog、MySQL `lower_case_table_names=1/2`（均有离线测试）。

## [1.0.0] - 2026-11-01

首个公开发布的正式版本。按功能归类，每条后面是对应的 PR。

### 采集与血缘

- 只读采集数据库元数据，按物理 ID 识别同一对象；解析视图、外部表，得到表级与字段级血缘（[#7](https://github.com/shenhui027-arch/avelis/pull/7)）。
- 导入 SQL 文件、粘贴 SQL、上传 Python / Shell 代码生成血缘；解析不了的片段由模型提出候选，人工确认。
- 批量血缘导入：按方言切分、语法树优先，支持 MERGE / UPDATE / 多路 INSERT 等写入类语句，临时表与中间表折叠，后台批次与进度，多文件与 zip，同一来源重新导入时替换旧版的边，模型辅助识别不规范脚本（[#48](https://github.com/shenhui027-arch/avelis/pull/48)，ADR 0016）；DELETE / UPDATE 只在不受限的整表操作时重置或替换来源（[#54](https://github.com/shenhui027-arch/avelis/pull/54)、[#59](https://github.com/shenhui027-arch/avelis/pull/59)）。
- 引擎适配层：StarRocks、PostgreSQL、ClickHouse、SQL Server、Oracle、Doris 支持采集、视图血缘、问数与执行前核对；MySQL 支持采集，作为外部表的追溯目标（[#7](https://github.com/shenhui027-arch/avelis/pull/7)）；Hive 支持元数据采集与视图列级血缘，配置的库不可见时整次采集失败、不误标删除（[#37](https://github.com/shenhui027-arch/avelis/pull/37)）。
- 表关联统一由治理维护；血缘页支持搜索、首屏可读、检查器可拖宽、字段可点（[#20](https://github.com/shenhui027-arch/avelis/pull/20)）。

### 口径治理与变更管理

- 指标与版本状态机、发布检查、复核与跨域批准；关联路径与粒度校验。
- 上游结构变化 → 影响分析 → 复核；受影响的口径由后端阻止继续使用。
- 口径的时间语义（编码时间字段、数据粒度、跨时间合成、口径级时区）、量纲与期望方向、数字体系，以及业务日历（[#50](https://github.com/shenhui027-arch/avelis/pull/50)，ADR 0014）。
- 变更留底：变更归因到数据库用户、检索与导出、历史只追加并封入哈希链、保留期可配置（[#9](https://github.com/shenhui027-arch/avelis/pull/9)，ADR 0008）。

### 智能问数

- LangGraph 问数智能体：多轮追问、会话持久化；模型只提交结构化意图，SQL 由已发布口径确定性生成并只读执行。
- 多指标、排名与前 N、环比 / 同比、派生指标（ADR 0005）。
- 问数对话按时间顺序展示，推理完整可见；可切换模型与思考强度（[#15](https://github.com/shenhui027-arch/avelis/pull/15)、[#16](https://github.com/shenhui027-arch/avelis/pull/16)）。

### 分析智能体

- 多步分析“为什么变了、什么关系、该怎么做”；取数只走受治理查询，报告中的数字逐个核验，结论分事实、计算、推断、建议（[#10](https://github.com/shenhui027-arch/avelis/pull/10)，ADR 0007、0009）。
- 证据发现、实时过程流与插话；对话确认与经验学习（[#12](https://github.com/shenhui027-arch/avelis/pull/12)，ADR 0013）。
- 数据集在主区域打开、换时间重跑、导出 CSV；历史读取按当前权限复查来源（[#19](https://github.com/shenhui027-arch/avelis/pull/19)）。
- 报告导出 PDF / HTML / Markdown，分简报与附带依据的完整版（[#21](https://github.com/shenhui027-arch/avelis/pull/21)）。
- 派生计算按公式推导量纲（同单位加减保留单位、相除得到比率），报告数字的单位与格式统一；文字里的识别符（如 V1、D5）不当作数字；分析方法的问题单独提示，不再把整份报告判为未通过。
- 分析可保存为定时任务，定时设置可视化选择（[#32](https://github.com/shenhui027-arch/avelis/pull/32)）。
- 续问在同一对话里接着分析，沿用此前各轮的结论与数据集；左侧对话支持重命名、归档、恢复与删除，证据按所属轮次打开（[#47](https://github.com/shenhui027-arch/avelis/pull/47)）。

### 界面

- 工作台、血缘、目录、口径、变更、问数、分析、设置等页面。
- 前端视觉 v2：玻璃质感、深浅主题、侧边栏（[#13](https://github.com/shenhui027-arch/avelis/pull/13)）；登录页与品牌标记（[#24](https://github.com/shenhui027-arch/avelis/pull/24)）；问数输入框底栏（[#31](https://github.com/shenhui027-arch/avelis/pull/31)）。
- 界面支持简体中文、繁体中文与英文，后端报错、导出标签与模型回答跟随界面语言（[#44](https://github.com/shenhui027-arch/avelis/pull/44)、[#45](https://github.com/shenhui027-arch/avelis/pull/45)、[#46](https://github.com/shenhui027-arch/avelis/pull/46)、[#58](https://github.com/shenhui027-arch/avelis/pull/58)，ADR 0015）。
- 目录详情页提供返回上一级；后台执行器没有运行时，排队中的分析会说明原因（[#47](https://github.com/shenhui027-arch/avelis/pull/47)）。

### 安全与权限

- 资产与口径按业务域授权；登录限流、首次登录改密、会话可吊销；登录时长可配置并在使用中自动续期（[#18](https://github.com/shenhui027-arch/avelis/pull/18)）。
- 数据源口令与模型密钥加密存储；治理动作写审计。
- 默认向模型发送查询结果与脱敏后的代码片段（分析需要），可在“设置 → 模型配置”关闭；保密场景建议接入本地模型。

### 部署

- 后端同时托管前端，单端口访问；自包含 Docker 镜像（运行期不联网下载）与 Docker Compose 编排（[#27](https://github.com/shenhui027-arch/avelis/pull/27)）。
- 部署配置文件 `deploy/docker/avelis.toml`（TOML，带中文注释），启动时校验并用中文指出问题（[#27](https://github.com/shenhui027-arch/avelis/pull/27)）；部署遗留问题修复（[#29](https://github.com/shenhui027-arch/avelis/pull/29)）。
- worker 心跳与 `/api/health/worker` 状态接口（[#47](https://github.com/shenhui027-arch/avelis/pull/47)）。

### 其他

- 产品代码去掉对演示数据的结构性假设（[#26](https://github.com/shenhui027-arch/avelis/pull/26)，ADR 0010）。
- 界面导览与真实截图（[#30](https://github.com/shenhui027-arch/avelis/pull/30)）；截图按界面语言分三套。
- 开源准备：社区文件、README 英文为主、文档重组与归档、代码整理与 CI、文档三语（[#33](https://github.com/shenhui027-arch/avelis/pull/33)、[#34](https://github.com/shenhui027-arch/avelis/pull/34)、[#36](https://github.com/shenhui027-arch/avelis/pull/36)、[#38](https://github.com/shenhui027-arch/avelis/pull/38)、[#42](https://github.com/shenhui027-arch/avelis/pull/42)）；GitHub 首页默认展示简体中文 README（[#57](https://github.com/shenhui027-arch/avelis/pull/57)）。
- 采用 Apache License 2.0。

### 已知局限

- 带时区列的业务时区换算只在编译出的 SQL 层面验证过，尚未在真实 ClickHouse / Oracle 上执行。
- 首次启动从配置文件登记的模型服务名称（如“（来自配置）”）是数据，不随界面语言变化，可在“设置 → 模型配置”里修改。

[未发布]: https://github.com/shenhui027-arch/avelis/compare/v1.0.4...HEAD
[1.0.4]: https://github.com/shenhui027-arch/avelis/compare/v1.0.3...v1.0.4
[1.0.3]: https://github.com/shenhui027-arch/avelis/compare/v1.0.2...v1.0.3
[1.0.2]: https://github.com/shenhui027-arch/avelis/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/shenhui027-arch/avelis/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/shenhui027-arch/avelis/releases/tag/v1.0.0
