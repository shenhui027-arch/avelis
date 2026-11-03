# 更新日志

本文件记录 Avelis 各版本的主要变化，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

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

### 已知局限

- Ossie 导出：Ossie 核心模型表达不了的语义（预聚合关联、仅用于过滤行的关联、带分组的固定过滤、跨时间期末值 / 平均值等）会拒绝导出该口径并说明原因（ADR 0017 第 6 节）；派生口径的两个边界尚未处理（[#88](https://github.com/shenhui027-arch/avelis/issues/88)）。
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

[未发布]: https://github.com/shenhui027-arch/avelis/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/shenhui027-arch/avelis/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/shenhui027-arch/avelis/releases/tag/v1.0.0
