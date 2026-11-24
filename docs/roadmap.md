# 开发路线图

[English](roadmap.en.md) | 简体中文 | [繁體中文](roadmap.zh-TW.md)

本文面向 Avelis 开发者与贡献者，记录基于 `v1.0.11` 基线推进的五阶段开发路线图（`M1` 至 `M5`）、严格执行顺序、工程边界与验收标准。**截至 `v1.1.0`（主干 `ce1fee4`），`M1` 至 `M4` 及认证/CI 收口已完成并合入 `main`，下一阶段 `M5` 处于计划状态**（不承诺具体发布日期）；已实现能力与系统结构见[架构与治理说明](architecture.md)、[可观测性与排障指南](observability.md)与[开发指南](development.md)。

## 执行顺序与边界约束

### 严格执行顺序

五阶段计划严格按 `M1 → M2 → M3 → M4 → M5` 顺序推进，**`M5` 仅在 `M1` 至 `M4` 全部完成并验收通过后实施**：

1. **M1（认证安全底座与商业化登录页重构，已完成）**：补齐身份认证、会话安全与权限隔离漏洞，并完成登录、改密与管理员用户运维界面重构（含 PR `#156` 会话撤销、`#149`–`#155` 及 `0032_username_ci_unique` 加固）。
2. **M2（界面体验、动效、可访问性与前端性能，已完成）**：在稳定认证外壳之上打通交互断链、补齐 CRUD 闭环、修复 WCAG AA 对比度与键盘无障碍，并完成路由与语言包拆包优化。
3. **M3（智能体能力跃迁，已完成）**：在完整交互链路之上推进混合检索、业务域经验共享、已核验查询库、多模型 SSE 流式与成本账本、分析探索沙箱与 MCP / 技能安全扩展。
4. **M4（数据安全合规、可观测性与工程化底座，已完成）**：补齐全部 8 个连接器的传输安全与只读隔离、密钥轮换、字段脱敏与行级权限、审计验真、结构化可观测与数据库集成 CI。
5. **M5（万表级规模重构与多副本高可用，下一阶段 · 计划）**：前置 `M1`–`M4` 门禁已满足，计划重构全局锁哈希链、万表级增量采集与检索索引、多副本并发 worker、跨副本 SSE、对象存储与云原生零停机部署。

### 硬边界与合规约束

| 约束类别 | 具体要求 |
|---|---|
| 原创实现与许可合规 | 不得照抄现有开源项目的代码行，须保持原创实现；可参考解析逻辑与架构思路，并可引入无商业化使用法律风险的预构建库或框架（如 MIT、Apache-2.0、BSD、ISC 等宽松商用许可） |
| 明确不做的集成与功能 | 不接入钉钉、飞书、企业微信私有 SDK；不实现 SAML 2.0；不实现 LDAP / AD 直连；不实现 SCIM 2.0；不实现自建 SMTP 发信找回密码；不开发公有云自助注册或计费功能 |
| 事实与基线区分 | 当前 `v1.0.11` 基线行为（例如未设初始口令时回退 `admin / 123456`、MCP 硬锁单一固定协议版本、全局 `history_chain_head` 行锁、本地技能目录与 Docker Compose 部署）由各阶段计划逐步演进替代，不与已发布基线混淆 |

## 阶段总览

| 阶段 | 主题 | 状态 | 前置依赖 | 核心目标 |
|---|---|---|---|---|
| M1 | 认证安全底座与商业化登录页重构 | 已完成 | `v1.0.11` 基线 | 清零鉴权与越权漏洞，迁移至 Cookie 认证，落地标准 OIDC / 可选 Forward Auth / 本地 TOTP 2FA / PAT，完成登录与改密页双主题重构及管理员用户运维（含 `#149`–`#155` 加固） |
| M2 | 界面体验、动效、可访问性与前端性能 | 已完成 | M1 | 打通跨页跳转、失败重试与任务/数据源 CRUD 闭环，修复全量令牌 WCAG AA 对比度与键盘/浮层无障碍，优化动效并削减主包体积 ≥ 50% |
| M3 | 智能体能力跃迁 | 已完成 | M2 | 落地混合检索、域共享经验与已核验查询库，解除单模型绑定并实现 SSE 流式与费用账本，完善只读沙箱、MCP 协商与技能审核（实机 LLM `scripts/eval_analysis.py` 按需复核） |
| M4 | 数据安全合规、可观测性与工程化底座 | 已完成 | M3 | 全 8 个连接器支持 TLS / CA 与只读会话，落地 `MultiFernet` 双密钥轮换、PII 脱敏、RLS、审计与哈希链验真 UI、JSON 日志与 `/metrics`、Postgres + Valkey CI 及 `sqlglot` 收敛 |
| M5 | 万表级规模重构与多副本高可用 | 下一阶段（计划） | M1–M4（已就绪） | 去除全局行锁改为批次或分片哈希链与游标流式清理，实现万表级增量采集与 `pg_trgm` / GIN 索引、多槽位隔离 worker、跨副本 SSE Pub/Sub、对象存储与 Helm 零停机部署 |

## M1：认证安全底座与商业化登录页重构（已完成）

当前 `v1.0.11` 基线中，未配置 `initial_admin_password` 时默认使用 `admin / 123456` 且不强制首次改密，部分接口缺少状态校验或归属校验。`M1` 计划完成以下能力：

### 前端登录页、改密页与管理员用户运维（计划）

- **登录页与改密页视觉重构**：重构 `frontend/src/pages/Login.tsx`、`frontend/src/styles/login.css` 与 `frontend/src/pages/account/ChangePassword.tsx`。左侧打造更具建筑感与数据叙事张力的展示舞台，呈现“血缘溯源 → 口径校验 → 归因结论”联动预览，并对浅色与深色双主题的材质与层次对比进行精调；右侧重构表单排版与聚焦微交互，修复空态主按钮对比度偏淡问题，并统一改密页与登录页设计语言。
- **商业化登录能力**：支持动态单槽位企业 OIDC 登录按钮（未配置时保持纯账密极简态，支持配置关闭本地登录）；支持轻白标配置（`brand_name`、`brand_logo_url`、`login_subtitle`、`terms_url`、`privacy_url`）；提供 7 天“记住登录”、全站密码框显隐切换、实时密码规则校验条，以及清晰的错误与锁定倒计时反馈。
- **管理员用户运维补齐**：在 `frontend/src/pages/settings/UsersPage.tsx` 补齐用户搜索、重置密码生成一次性可复制临时口令弹窗、强制踢下线与 `create.isPending` 防重复提交；在 `frontend/src/pages/catalog/DomainDetail.tsx` 补齐移除成员按钮；在 `frontend/src/layout/Shell.tsx` 按角色隐藏无权访问的设置标签页。

### 后端安全漏洞清零与轻量标准 SSO（计划）

- **强制改密与越权修复**：在 `backend/src/avelis/api/deps.py` 强制校验 `user.must_change_password`（仅放行 `/api/me`、`/api/me/password`、`/api/auth/logout`），封堵 `/oauth/consent` 绕过；在 `backend/src/avelis/bootstrap.py` 淘汰无感默认 `admin / 123456`，改为强制首次改密或在未设环境变量时生成一次性随机密码写入启动日志；修复 `backend/src/avelis/api/datasources.py` 中 `GET /api/jobs/{job_id}` 的 IDOR 越权；在 `backend/src/avelis/api/catalog.py` 补齐 `DELETE /api/domains/{domain_id}/members/{user_id}`。
- **HTTP 安全头与生产文档开关**：在 `backend/src/avelis/api/app.py` 新增 HTTP 安全头中间件（CSP、`X-Frame-Options: DENY`、`X-Content-Type-Options: nosniff`、`Referrer-Policy`、条件 `HSTS`），并在生产模式下默认关闭 `/api/docs` 与 `/api/openapi.json`。
- **防爆破、Redis 容错与 Cookie 认证**：在 `backend/src/avelis/api/auth.py` 与 `backend/src/avelis/cache.py` 落地原子 Redis `MULTI/EXEC` 计数、账号/IP 双维度锁定、恒定耗时防 Argon2 时序枚举、用户名大小写归一，以及 `/api/me/password` 限流；鉴权路径捕获 `RedisError` 防止单点闪断引发全站 500。认证载体迁移至 `HttpOnly + Secure + SameSite=Lax` Cookie（覆盖 `frontend/src/api/client.ts` 与后端认证端点），同时保留 Bearer 令牌兼容。
- **轻量标准认证扩展**：仅实现 1 套通用标准 OIDC（Authorization Code + PKCE + 自动建号 JIT，直接复用 `backend/pyproject.toml` 已安装的 `authlib`、`pyjwt`、`httpx`），加上可选约 20 行反向代理受信任请求头（Forward Auth）、可选纯本地 TOTP 双因素认证与个人访问令牌（PAT）。

### M1 验收重点（计划）

- 后端安全测试覆盖并全部通过上述漏洞修复、防爆破、Redis 容错与 Cookie/Bearer 双栈认证，以及 OIDC / Forward Auth / TOTP / PAT 与管理员运维接口。
- 独立评审代理依据视觉与交互评分表，对登录页与改密页在浅色/深色双主题及桌面端/移动端 Playwright 截图进行评审并通过。

## M2：界面体验、动效、可访问性与前端性能（已完成）

`M2` 聚焦于打通前端交互断链、补齐治理管理闭环、修复全站 WCAG AA 对比度与键盘无障碍，并显著降低首屏打包体积与渲染回流。

### 交互断链、死胡同与 CRUD 闭环修复（计划）

- **跨页跳转、重试与导出闭环**：修复 `frontend/src/pages/catalog/MetricDetail.tsx` 跳转 `/ask?metric_id=...` 丢参数断链并在 `/analysis` 读取预置上下文，统一并收敛旧版 `/ask/:threadId` 与 `/analysis` 入口；在 `frontend/src/pages/analysis/AnalysisPage.tsx` 的失败与停止态补齐一键重试和编辑重发，为同文件中的 `LookupAnswer` 补齐导出、复制 SQL 与“深入分析”入口。
- **全局组件与客户端健壮性**：为 `frontend/src/components/ui.tsx` 的全局 `Code` 组件补齐一键复制 SQL；统一提问与追问提交快捷键；修复 `frontend/src/api/client.ts` 中 `logout()` 缺少 `try/finally` 的清理缺陷，收敛 5 处裸 `fetch()` 调用，透传 React Query `AbortSignal`，增加局部 `ErrorBoundary` 并修复 `RouteError`。
- **导航效率与工作台体验**：新增 `⌘K / Ctrl+K` 全局命令面板；为 `frontend/src/pages/Workbench.tsx` 增加冷启动新手向导；补齐路由级 `document.title`、页面面包屑与侧栏多数据源异常汇总。
- **管理 CRUD 与细节打磨**：补齐定时任务（编辑、删除、运行历史、保留模型与技能、按行独立 loading）与数据源（编辑、删除、测试连接）完整 CRUD；`frontend/src/pages/changes/ChangesPage.tsx` 展示全部 `definition_changed` 变更对照；`frontend/src/pages/changes/ChangeRecords.tsx` 补齐筛选重置与 URL 状态同步；分析证据栏 `D3–D8` 区分维度标签、血缘边标签防重叠（`frontend/src/pages/lineage/LineagePage.tsx`）、侧栏最近对话渐变与“查看全部”、技能卡人类可读标题；修复 `frontend/src/numbers.ts` 在英文与繁中下输出简体“万/亿”以及 `frontend/src/pages/ask/presentation.ts` 仅匹配中文表头的正则缺陷。

### 设计令牌对比度、无障碍与动效性能优化（计划）

- **设计系统与 WCAG AA 对比度**：修复 `frontend/design/tokens.json` 浅色/深色语义令牌的 WCAG AA 对比度（覆盖 `--ok` 与 `--ok-subtle`、`--warn`、`--accent-text`、`--danger`、`--text-tertiary` 等），并扩展 `frontend/src/styles/contrast.test.mjs` 校验全量语义令牌。
- **键盘与屏幕阅读器无障碍（A11y）**：实现统一浮层栈以修复 `Drawer` 与 `ConfirmDialog`/`Select` 同时打开时按 `Esc` 双层关闭的缺陷；`ConfirmDialog` 危险操作默认聚焦取消按钮并补齐 `aria-describedby`；修复 `DateInput` Tab 焦点陷阱与 ARIA grid 语义并支持年月快选或输入；菜单补齐方向键导航与焦点归还；`Banner` 补齐 `role="alert"` / `role="status"`；新增 Skip Link 与全局 `:focus-visible`。
- **动效收敛与前端性能**：解决 `frontend/src/styles/app.css` 与 `frontend/src/styles/pages.css` 中 `@keyframes av-shimmer` 同名覆盖冲突；替换会触发 `width`、`flex-grow` 或 `background-position` 布局重绘的动画；节流优化 `frontend/src/pages/analysis/RunTranscript.tsx` 的 `useReveal` 以避免 60fps 回流；移除工作台三层叠加重复动效；修复 `Tooltip` 首帧方向偏差与 `prefers-reduced-motion` 漏网项；在 `frontend/src/main.tsx` 实施路由级 `React.lazy` 拆包与语言包按需加载（将约 1.21 MB 主包体积削减 ≥ 50%）；在 `LineagePage.tsx` 拆分 `dagre.layout` 计算与选中态；停止侧栏对已归档对话的全局轮询；修复窄屏 `.av-fill` 滚动裁切并将 `100vh` 替换为 `100dvh`。

### M2 验收重点（计划）

- 扩展后的 `contrast.test.mjs` 覆盖全量浅/深色语义令牌并全部通过 WCAG AA 校验。
- 前端单测与 Playwright E2E 覆盖并验证全部交互闭环、CRUD、i18n 与键盘无障碍规则；`vite build` 主包体积较 1.21 MB 削减 ≥ 50%。

## M3：智能体能力跃迁（已完成）

`M3` 计划全面升级问数与分析智能体的检索精度、多模型生态、流式交互、成本治理、评测得分与 MCP / 技能安全。

### 检索增强与组织级知识飞轮（计划）

- **混合检索与已核验查询库（VQR）**：在 `backend/src/avelis/ask/service.py`、`backend/src/avelis/analysis/tools.py` 与 `backend/src/avelis/analysis/retrieval.py` 落地语义向量与词法回退相结合的混合检索；新增 Golden SQL / Verified Query Repository（VQR）。
- **域共享经验与问数闭环**：将 `backend/src/avelis/governance/experiences.py` 的个人经验升级为支持团队与业务域复核共享（含禁止自审）；问数模式补齐赞/踩反馈、长对话压缩、解读数字与查询结果核验，以及可选语义缓存。

### 多模型生态、实时流式与成本账本（计划）

- **解耦模型协议与故障降级**：解除 `backend/src/avelis/modelcfg.py` 与 `backend/src/avelis/llm_protocol.py` 对 DeepSeek 的单一绑定，统一适配 OpenAI、Anthropic 及兼容接口的流式推理与思考强度控制；支持主备模型自动降级与按任务复杂度路由。
- **全链路 SSE 流式与用量账本**：前后端为问数与分析建立统一 SSE 实时事件流；补齐覆盖问数与分析的全量 token 与费用账本、单价配置以及按用户/业务域的配额与限流。

### 分析深度扩展、评测达标与 MCP / 技能完善（计划）

- **评测体系与 ≥ 70% 通过率目标**：基于 `scripts/eval_analysis.py` 重点攻克探索题（基线 12.5%）与负样本题（基线 25.0%），将分析整体通过率提升至 ≥ 70%，并补充问数评测集与数字核验测试。
- **只读沙箱与多引擎探索查询**：扩展 `backend/src/avelis/analysis/explore.py` 的 `query_sql` 支持全部查询引擎（当前 `v1.0.11` 仅支持部分引擎）并支持窗口函数；在受控只读 Python / DuckDB 沙箱内完成复杂计算；在 `backend/src/avelis/analysis/agent.py` 支持只读工具并发执行。
- **MCP 协议协商与技能安全**：修复 `backend/src/avelis/mcp/auth.py` 硬锁单一固定 `Mcp-Protocol-Version` 的限制，改为向下兼容协商；增强 `backend/src/avelis/mcp/tools.py` 中 `ask_metric` 的多指标、排序与筛选取值对齐；在 `backend/src/avelis/skills/` 禁止管理员自审自己上传的技能包，增加上传限流与 Prompt 注入/敏感词预检。

## M4：数据安全合规、可观测性与工程化底座（已完成）

`M4` 补齐企业生产环境所需的数据传输加密、隐私脱敏、合规审计、可观测性指标及数据库集成测试流水线。

### 连接器加固、密钥轮换与数据脱敏（计划）

- **全 8 个连接器 TLS 与只读加固**：为 `backend/src/avelis/connectors/` 全部 8 个数据源连接器（StarRocks、Doris、MySQL、PostgreSQL、ClickHouse、SQL Server、Oracle、Hive）补齐 TLS/SSL 与 CA 证书配置；为 StarRocks、Doris、ClickHouse 补齐会话级只读控制。
- **双密钥轮换与 PII / RLS 治理**：在 `backend/src/avelis/security.py` 与 `backend/src/avelis/bootstrap.py` 实现真正的 `MultiFernet` 双密钥轮换 CLI；支持资产字段级 PII 标记并在界面展示与模型外发前脱敏；对齐 `send_results` 默认安全策略；支持基础行级权限（RLS）表达式注入。

### 审计扩展、哈希链验真与留存清理（计划）

- **审计上下文与导出留痕**：为 `AuditEvent` 补齐 `client_ip`、`user_agent` 与 `request_id`；记录答案查看及 CSV / PDF / HTML 导出审计事件；扩展安全 Webhook 事件类型；上线前端管理员审计日志查询与哈希链验真页面。
- **合规清理与匿名化**：在 `backend/src/avelis/history/retention.py` 实现 `Answer` 与 `AnalysisDataset` 保留期自动清理与离职/注销账号匿名化。

### 可观测性、CI 流水线与依赖收敛（计划）

- **日志、指标与链路追踪**：落地结构化 JSON 日志、Prometheus `/metrics` 指标端点与可选 OpenTelemetry 接入。
- **全量集成 CI 与依赖收敛**：在 `.github/workflows/ci.yml` 增加 PostgreSQL 与 Valkey 服务容器，自动执行 `backend/tests/db` 与 `backend/tests/metadb` 套件、Docker 冒烟测试、安全漏洞扫描与 Playwright E2E；收敛 `backend/pyproject.toml` 中的 `sqlglot` 个人 Fork 依赖。

## M5：万表级规模重构与多副本高可用（计划，最后阶段）

> **前置门禁**：`M5` 必须严格在 `M1`、`M2`、`M3`、`M4` 全部完成并通过验收之后实施。

当前 `v1.0.11` 采用单点全局 `history_chain_head` 行锁、`LOCK TABLE` 清理、全量图指纹重算以及单副本本地技能存储。`M5` 计划面向万表级元数据规模与多副本高可用生产环境完成以下重构：

### 哈希链与历史清理扩展性（计划）

- **无全局行锁哈希链**：将 `backend/src/avelis/history/integrity.py` 中逐行锁全局单行 `history_chain_head FOR UPDATE` 的触发器机制重构为批次/事务级 Merkle 锚定或分片哈希链。
- **游标流式验真与清理**：改造 `backend/src/avelis/history/retention.py` 的验真与保留期清理逻辑为游标流式处理，移除 `LOCK TABLE` 阻塞锁。

### 万表级目录采集与检索（计划）

- **增量采集与局部子图重算**：实现元数据增量采集、批量 Upsert 与局部受影响子图的血缘及校验码重算，避免全量重算开销。
- **全文与模糊检索索引**：落地 PostgreSQL `pg_trgm` 目录检索索引与 `Answer` GIN 索引；优化 Apache Hive 批量元数据拉取与目标库连接复用。

### 多副本高可用与云原生部署（计划）

- **多槽位隔离 worker 与跨副本 SSE**：在 `backend/src/avelis/jobs.py` 与 `backend/src/avelis/cache.py` 实现多并发槽位、队列隔离 worker 与按实例 ID 心跳；消除同步阻塞 `?wait=true` 查询，基于 Redis Pub/Sub 实现跨副本 SSE 实时推送。
- **对象存储与 Helm 零停机部署**：为技能包与导出产物增加 S3 / MinIO 对象存储后端；提供 Kubernetes Helm Chart（`deploy/helm/`）、Pre-deploy Migration Job、`CREATE INDEX CONCURRENTLY` 零阻塞索引迁移，以及应用优雅停机时的数据库连接池与 Redis 连接释放。

## 持续演进方向（计划）

除上述五个顺序推进的工程阶段外，以下已公布的演进方向将在对应阶段或后续迭代中持续推进，同样不承诺日期：

- **更多引擎的外部 Catalog 与物化视图**：持续扩展跨引擎外部表与物化视图的元数据采集和血缘解析。
- **问数表达能力增强**：支持派生指标嵌套、按周 / 月时间粒度以及占比计算。

## 验收与参考

| 验证资源或参考文档 | 作用 |
|---|---|
| [`frontend/design/tokens.json`](../frontend/design/tokens.json) 与 [`contrast.test.mjs`](../frontend/src/styles/contrast.test.mjs) | 浅色/深色全量语义令牌 WCAG AA 对比度校验 |
| [`scripts/eval_analysis.py`](../scripts/eval_analysis.py) | 分析智能体评测脚本（`M3` 整体通过率 ≥ 70% 基线门禁） |
| [`backend/tests/db`](../backend/tests/db) 与 [`backend/tests/metadb`](../backend/tests/metadb) | PostgreSQL 与元数据库集成测试套件（`M4` 接入 CI 容器流水线） |
| [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) | 仓库代码规范、单测、构建及后续集成/安全/E2E 流水线定义 |
| [架构与治理说明](architecture.md) 与 [ADR 0001–0022](decisions/) | 当前 `v1.0.11` 基线架构设计与历史决策上下文 |
