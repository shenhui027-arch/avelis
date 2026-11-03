# 配置参考

[English](configuration.en.md) | 简体中文 | [繁體中文](configuration.zh-TW.md)

Avelis 的配置可以写在**配置文件**（TOML）里，也可以用 **`AVELIS_*` 环境变量**或 **`.env` 文件**设置。生产部署推荐配置文件；本地开发沿用仓库根目录的 `.env`。

## 加载顺序

同一项在多处出现时，按以下优先级取值（高 → 低）：

1. 进程环境变量 `AVELIS_*`
2. `.env` 文件：默认仓库根目录的 `.env`，可用 `AVELIS_ENV_FILE` 指定（相对路径按仓库根目录解析）
3. 配置文件：`AVELIS_CONFIG` 指定的文件；未指定时若存在 `/etc/avelis/avelis.toml` 则读取
4. 内置默认值

- `AVELIS_CONFIG` 指定了但文件不存在或无法解析，启动失败并说明原因。
- 配置文件里出现未知的节或键（多半是拼写错误）也会报错，并提示最接近的合法键名。
- Docker 镜像约定配置文件挂载到容器内的 `/etc/avelis/avelis.toml`，部署编排已设置好 `AVELIS_CONFIG`。
- 样例文件：[deploy/docker/avelis.example.toml](../deploy/docker/avelis.example.toml)（每项都有中文注释与默认值）。

### 检查配置

```bash
cd backend && uv run python -m avelis.config check                 # 本机
docker compose run --rm app python -m avelis.config check          # Docker 部署（在 deploy/docker 下执行）
```

输出每一项的生效值与来源（环境变量 / .env / 配置文件 / 默认），口令与密钥显示为 `******`。启动时同样会校验：缺必填项或格式错误时打印中文说明（哪一项、当前来自哪里、怎么改）并以退出码 2 退出，不打印堆栈。

## 敏感项

口令与密钥允许明文写在配置文件里，但请注意：

- 配置文件与 `.env` 都要限制读取权限（例如 `chmod 640`，属组为容器用户 UID 10001 可读），不要提交到代码仓库，不要贴进聊天或工单。
- `AVELIS_SECRET` 务必与元数据库一起备份：丢失后已保存的数据源口令与模型密钥无法解密。
- 更推荐的做法是**文件引用**：把口令放在单独的文件里（如 Docker / Kubernetes secrets），配置里只写路径。

| 写法 | 示例 |
|---|---|
| 配置文件里的明文 | `[database]` `password = "…"` |
| 配置文件里的文件引用 | `[database]` `password_file = "/run/secrets/avelis_db_password"` |
| 环境变量明文 | `AVELIS_DB_PASSWORD=…` |
| 环境变量文件引用 | `AVELIS_DB_PASSWORD_FILE=/run/secrets/avelis_db_password` |

支持文件引用的项：`AVELIS_SECRET`、`AVELIS_DB_PASSWORD`、`AVELIS_REDIS_PASSWORD`、`AVELIS_INITIAL_ADMIN_PASSWORD`、`AVELIS_MODEL_API_KEY`、`AVELIS_DATABASE_URL`、`AVELIS_REDIS_URL`。同一层里明文与文件引用同时出现会报错；不同层之间按[加载顺序](#加载顺序)的优先级覆盖（例如环境变量 `AVELIS_DB_PASSWORD_FILE` 覆盖配置文件里的明文）。

## 配置项

“配置文件”一列是 TOML 中的 `[节] 键`。“必填”指没有可用默认值、必须由部署者填写的项。

### 安全 `[security]`

| 配置文件 | 环境变量 | 默认值 | 说明 |
|---|---|---|---|
| `secret` | `AVELIS_SECRET` | —（**必填**） | 主密钥，至少 16 个字符；加密数据源口令与模型密钥、签发登录令牌。上线后不要更换 |
| `access_token_minutes` | `AVELIS_ACCESS_TOKEN_MINUTES` | `120` | 单个登录凭证有效期（闲置超时），分钟 |
| `session_max_hours` | `AVELIS_SESSION_MAX_HOURS` | `168` | 一次登录最长可续期时长，小时 |
| `login_max_failures` | `AVELIS_LOGIN_MAX_FAILURES` | `5` | 同一账号 + 来源地址连续失败多少次后锁定 |
| `login_lock_minutes` | `AVELIS_LOGIN_LOCK_MINUTES` | `15` | 锁定时长，分钟 |
| `allow_self_review` | `AVELIS_ALLOW_SELF_REVIEW` | `false` | 是否允许提交人复核自己的变更 |

### 元数据库 `[database]`

元数据库只支持 PostgreSQL。连接方式三选一，优先级从高到低：完整连接串 `url` → 分项（填了 `host` 时）→ 本地演示默认（`127.0.0.1` + `AVELIS_PG_PORT` / `AVELIS_PG_PASSWORD`，只用于开发）。

| 配置文件 | 环境变量 | 默认值 | 说明 |
|---|---|---|---|
| `url` | `AVELIS_DATABASE_URL` | 空 | 完整连接串 `postgresql+psycopg://用户:口令@主机:端口/库名`；填写后忽略分项 |
| `host` | `AVELIS_DB_HOST` | 空 | 主机；Docker 内置服务为 `postgres` |
| `port` | `AVELIS_DB_PORT` | `5432` | 端口 |
| `name` | `AVELIS_DB_NAME` | `avelis` | 库名 |
| `user` | `AVELIS_DB_USER` | `avelis` | 账号（需能在该库建表与模式） |
| `password` | `AVELIS_DB_PASSWORD` | —（用分项时**必填**） | 口令 |
| `sslmode` | `AVELIS_DB_SSLMODE` | 空（驱动默认 `prefer`） | `disable` / `allow` / `prefer` / `require` / `verify-ca` / `verify-full` |
| `pool_size` | `AVELIS_DB_POOL_SIZE` | `5` | 每个进程的连接池大小 |
| `max_overflow` | `AVELIS_DB_MAX_OVERFLOW` | `10` | 连接池临时溢出上限 |
| `pool_recycle_seconds` | `AVELIS_DB_POOL_RECYCLE_SECONDS` | `1800` | 连接回收周期，秒 |

### Redis `[redis]`

Redis 协议服务（Redis、Valkey 均可）。优先级：`url` → 分项（填了 `host` 时）→ 本地演示默认（`127.0.0.1` + `AVELIS_REDIS_PORT` / `AVELIS_REDIS_PASSWORD`）。

| 配置文件 | 环境变量 | 默认值 | 说明 |
|---|---|---|---|
| `url` | `AVELIS_REDIS_URL` | 空 | 完整连接串 `redis://:口令@主机:端口/0` |
| `host` | `AVELIS_REDIS_HOST` | 空 | 主机；Docker 内置服务为 `redis` |
| `port` | `AVELIS_REDIS_PORT` | `6379`（未填 `host` 时的演示默认为 `16379`） | 端口 |
| `db` | `AVELIS_REDIS_DB` | `0` | 库号 |
| `password` | `AVELIS_REDIS_PASSWORD` | 空 | 口令（Docker 内置服务**必填**） |
| `ssl` | `AVELIS_REDIS_SSL` | `false` | 是否使用 TLS（`rediss://`） |

### 服务 `[server]`

| 配置文件 | 环境变量 | 默认值 | 说明 |
|---|---|---|---|
| `host` | `AVELIS_HOST` | `127.0.0.1`（镜像内 `0.0.0.0`） | 监听地址 |
| `port` | `AVELIS_PORT` | `8000` | 监听端口（Docker 对外端口在 Compose 里设置） |
| `workers` | `AVELIS_WORKERS` | `1` | 进程数（uvicorn workers） |
| `public_url` | `AVELIS_PUBLIC_URL` | 空（按请求推导 MCP 对外地址） | 对外访问地址，如 `http://10.0.0.5:8080`；配置后用于启动日志、跨域白名单与 OAuth issuer，详见下节 |
| `frontend_dir` | `AVELIS_FRONTEND_DIR` | 空（镜像内 `/app/frontend/dist`） | 构建好的前端目录；设置后同一端口托管前端，SPA 路由回退到 `index.html`。相对路径按仓库根目录解析 |
| `forwarded_allow_ips` | `AVELIS_FORWARDED_ALLOW_IPS` | `127.0.0.1` | uvicorn 信任的代理地址，用于处理 `X-Forwarded-For` 与 `X-Forwarded-Proto`（逗号分隔，`*` 为全部） |
| `log_level` | `AVELIS_LOG_LEVEL` | `info` | `critical` / `error` / `warning` / `info` / `debug` |
| `cors_origins` | `AVELIS_CORS_ORIGINS` | `http://127.0.0.1:5173`、`http://localhost:5173` | 跨域白名单（配置文件里写数组，环境变量逗号分隔）；同一端口访问时不需要 |

### MCP 服务 `[mcp]`

MCP 与 Avelis 后端在同一进程运行，默认开启，部署后可直接使用“设置 → MCP 接入”，无需另开开关。不需要时可显式设置 `AVELIS_MCP_ENABLED=false`（或 `[mcp] enabled = false`）关闭；此时 `/mcp`、OAuth 协议端点和 `/api/oauth/*` 管理接口均返回 404，即使配置了前端 SPA 托管也不会回退到前端页面。

| 配置文件 | 环境变量 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | `AVELIS_MCP_ENABLED` | `true` | 启用 MCP 服务及内置 OAuth 授权服务器；显式设为 `false` 可关闭 |
| `access_token_seconds` | `AVELIS_MCP_ACCESS_TOKEN_SECONDS` | `900` | OAuth 访问令牌有效期，秒（15 分钟），须为正整数 |
| `refresh_token_seconds` | `AVELIS_MCP_REFRESH_TOKEN_SECONDS` | `2592000` | OAuth 刷新令牌有效期，秒（30 天），须为正整数；每次刷新轮换令牌 |
| `cimd_cache_ttl_seconds` | `AVELIS_MCP_CIMD_CACHE_TTL_SECONDS` | `3600` | CIMD 客户端元数据缓存有效期，秒；过期后重新拉取，须为正整数 |
| `cimd_allowed_urls` | `AVELIS_MCP_CIMD_ALLOWED_URLS` | `[]` | 本地开发 CIMD 地址的精确允许列表（配置文件里写数组，环境变量逗号分隔）；只接受完整的 `http(s)://127.0.0.1:PORT/path`，不允许任意私网地址或通配符 |

`[server] public_url`（`AVELIS_PUBLIC_URL`）可选。配置时沿用现有规范化规则，不能含凭据、查询参数或片段；OAuth `issuer` 使用规范化后的地址，资源 URI 为它直接拼接 `/mcp`，不额外去掉末尾斜杠。未配置时，每次请求从经 uvicorn 代理头处理后的 scheme 与合法的 Host（含可选端口）组成 origin，再加 ASGI `root_path` 得到 `issuer`；资源 URI 为 `issuer + "/mcp"`。例如 `http://avelis.test:8080` 与 `root_path = "/avelis"` 得到 issuer `http://avelis.test:8080/avelis`、resource `http://avelis.test:8080/avelis/mcp`。包含路径、用户信息或非法字符的 Host 返回 400。

AS 元数据、受保护资源元数据（PRM）、授权响应的 `iss`、前端确认页地址与 `WWW-Authenticate` 的 `resource_metadata` 均使用本次请求的同一组地址。授权、换码、刷新时提交的 `resource` 必须逐字匹配；令牌仍绑定签发时的 resource，换用其他主机名访问会被拒绝，应在完整流程中保持同一访问地址。

生产环境必须使用 HTTPS。反向代理须保留客户端的 Host（含端口）及公开路径，并正确传递协议；用 `[server] forwarded_allow_ips`（`AVELIS_FORWARDED_ALLOW_IPS`）配置可信代理，或直接配置客户端实际访问的 `AVELIS_PUBLIC_URL`。后端只使用 uvicorn 按现有信任规则处理后的 scheme，不额外信任 `X-Forwarded-Host` 等转发头。本机 HTTP 开发或测试若遇到 Authlib 拒绝不安全传输，须显式设置 `AUTHLIB_INSECURE_TRANSPORT=1`。这是 Authlib 的标准环境变量，仅用于本机开发与测试，不是 `AVELIS_*` 配置项；服务不会无条件关闭该安全检查。

开发环境的 `frontend/vite.config.ts` 为 `/mcp`、OAuth 协议端点及 `/.well-known` 设置 `changeOrigin: false`，5173 → 8000 代理保留浏览器的 Host，因此未配置 `public_url` 时会推导为浏览器看到的 `http://127.0.0.1:5173`（或实际使用的主机名）。若自行改为 `changeOrigin: true`，Host 会变成后端地址，建议显式设置 `AVELIS_PUBLIC_URL` 为浏览器访问的地址。

默认提供以下路径：

- `/mcp`：无状态 Streamable HTTP，要求 `Mcp-Protocol-Version: 2026-07-28`。
- `/.well-known/oauth-authorization-server`、`/.well-known/oauth-protected-resource/mcp`：授权服务器与受保护资源元数据。
- `/oauth/authorize`、`/oauth/token`、`/oauth/revoke`、`/oauth/register`：授权、换码与刷新、令牌撤销，以及兼容旧客户端的动态注册。
- `/api/oauth/consent/{ticket}` 及其 `/approve`、`/deny`：使用现有 Avelis Bearer 登录令牌读取并确认授权；`/oauth/authorize` 将客户端请求转到前端 `/oauth/consent?ticket=…`，确认票据有效期为 5 分钟且只能使用一次。
- `/api/oauth/authorizations`、`/api/oauth/authorizations/{id}`：查看、撤销当前用户自己的授权；`/api/oauth/clients`、`/api/oauth/clients/{id}/revoke`：管理员查看、禁用客户端。

CIMD 默认只拉取 HTTPS 文档，所有解析地址都须为公网地址，并固定连接到已检查的 IP；总超时 3 秒，文档上限 5 KiB，拒绝重定向、压缩和代理。`cimd_allowed_urls` 只为列出的本机开发地址放行 HTTP 与回环地址，不放宽其他拉取限制。授权码、确认票据与令牌在数据库中只保存哈希；授权撤销、客户端禁用或检测到授权码／刷新令牌重放后，相应令牌失效。

MCP 工具使用令牌所代表用户的现有业务域权限，并受授权的 `metrics:read`、`lineage:read`、`ask:execute` 范围约束。`ask_metric` 复用页面问数的编译器、查询超时与行数上限，默认最多返回 200 行；遵循现有数据外发设置（`[model] send_results` / `AVELIS_MODEL_SEND_RESULTS`），关闭时只返回口径定义和编译后的查询说明，不返回数据行。

### 初始化 `[bootstrap]`

| 配置文件 | 环境变量 | 默认值 | 说明 |
|---|---|---|---|
| `auto_migrate` | `AVELIS_AUTO_MIGRATE` | `true` | `avelis.server` 启动时自动迁移元数据库（PostgreSQL 咨询锁串行）；显式设为 `false` 可关闭 |
| `auto_init_admin` | `AVELIS_AUTO_INIT_ADMIN` | `true` | 启动时创建首个管理员（库里已有启用的管理员时跳过）；显式设为 `false` 可关闭 |
| `initial_admin_username` | `AVELIS_INITIAL_ADMIN_USERNAME` | `admin` | 首个管理员账号 |
| `initial_admin_password` | `AVELIS_INITIAL_ADMIN_PASSWORD` | 空（使用 `123456`） | 可选；未配置或留空时使用默认口令 `123456`，不校验强度、不强制改密，请登录后在界面修改。显式填写时须为 12–128 个字符、不含账号、至少 4 种不同字符，首次登录强制改密 |

自动迁移与管理员初始化默认开启，即使未写 `[bootstrap]` 段也使用上述规则，不区分环境或元数据库是否位于本机。默认账号为 `admin`，可通过 `initial_admin_username` 修改。已有启用的管理员时跳过，不修改已有账号；同名非管理员用户已存在时仍报错，不修改该用户。

### 业务 `[business]`

| 配置文件 | 环境变量 | 默认值 | 说明 |
|---|---|---|---|
| `timezone` | `AVELIS_BUSINESS_TIMEZONE` | `Asia/Shanghai` | 业务时区 |
| `number_style` | `AVELIS_NUMBER_STYLE` | `cn` | 数字显示体系：`cn` 使用万、亿；`intl` 使用 K、M、B，报告正文与导出共用，应用设置接口同时提供给前端图表 |
| `unassigned_asset_policy` | `AVELIS_UNASSIGNED_ASSET_POLICY` | `deny` | 未归属业务域的资产：`deny`（只有管理员与维护者可见）/ `allow` |
| `require_column_comments` | `AVELIS_REQUIRE_COLUMN_COMMENTS` | `false` | 口径依赖的列缺数据库注释时是否阻断发布（否则只提示） |

`number_style` 按换算回基本单位后的绝对值选择缩写：`cn` 在 ≥ `1e8` 时用亿、≥ `1e4` 时用万；`intl` 在 ≥ `1e9` 时用 B、≥ `1e6` 时用 M、≥ `1e4` 时用 K（除以 `1e3`）。两种体系都从 `1e4` 起缩写，因此 `1e3` 至小于 `1e4` 的数仍按普通数字显示。百分比、百分点、带单位的比率和未知量纲保持原有格式。

### 问数与查询 `[query]`

| 配置文件 | 环境变量 | 默认值 | 说明 |
|---|---|---|---|
| `timeout_seconds` | `AVELIS_QUERY_TIMEOUT_SECONDS` | `30` | 单条查询超时，秒 |
| `mem_limit_bytes` | `AVELIS_QUERY_MEM_LIMIT_BYTES` | `2147483648` | 单条查询内存上限（支持的引擎），字节 |
| `max_rows` | `AVELIS_QUERY_MAX_ROWS` | `1000` | 单条查询返回行数上限 |
| `answer_cache_seconds` | `AVELIS_ANSWER_CACHE_SECONDS` | `300` | 答案缓存时长，秒 |
| `live_check` | `AVELIS_ASK_LIVE_CHECK` | `true` | 问数执行前核对来源结构是否与上次采集一致 |
| `max_tool_rounds` | `AVELIS_ASK_MAX_TOOL_ROUNDS` | `8` | 问数智能体一次回答中最多的工具调用轮数 |

### 采集与后台任务 `[collect]` `[worker]` `[history]` `[import]`

| 配置文件 | 环境变量 | 默认值 | 说明 |
|---|---|---|---|
| `[collect] interval_minutes` | `AVELIS_COLLECT_INTERVAL_MINUTES` | `15` | 新建数据源的默认采集间隔，分钟（每个数据源可单独修改） |
| `[collect] ddl_audit_slack_seconds` | `AVELIS_DDL_AUDIT_SLACK_SECONDS` | `5` | DDL 审计归因窗口两端的时钟容差，秒 |
| `[worker] schedule_seconds` | `AVELIS_WORKER_SCHEDULE_SECONDS` | `60` | 定时扫描间隔，秒 |
| `[worker] stale_seconds` | `AVELIS_WORKER_STALE_SECONDS` | `30` | 排队多久未执行的任务由扫描补执行，秒 |
| `[worker] running_timeout_seconds` | `AVELIS_WORKER_RUNNING_TIMEOUT_SECONDS` | `3600` | 运行超时记为失败（不自动重跑），秒 |
| `[history] retention_days` | `AVELIS_HISTORY_RETENTION_DAYS` | `1095` | 变更留底保留天数，`0` 为不清理 |
| `[history] purge_interval_hours` | `AVELIS_HISTORY_PURGE_INTERVAL_HOURS` | `24` | 超期历史清理间隔，小时 |
| `[history] export_max_rows` | `AVELIS_HISTORY_EXPORT_MAX_ROWS` | `50000` | 变更记录一次导出的行数上限 |
| `[import] max_bytes` | `AVELIS_IMPORT_MAX_BYTES` | `2097152` | 导入血缘时单个文件或粘贴内容的上限，字节 |

超时采集和导入的治理收尾会在后续扫描中重试，业务任务本身不会自动重跑。Webhook 投递每轮共用固定 5 秒预算，单次请求最多 5 秒、连接最多 3 秒；连续失败两次的订阅在本轮跳过剩余条目。此预算暂无环境变量或 TOML 配置入口，详见[事件投递规则](events.md#投递与签名验证)。

### 分析智能体 `[analysis]`

每次分析的预算；到达任一上限后停止取数并收尾成报告。

| 配置文件 | 环境变量 | 默认值 | 说明 |
|---|---|---|---|
| `seconds` | `AVELIS_ANALYSIS_SECONDS` | `300` | 时长上限，秒（同步分析接口另留 120 秒收尾余量） |
| `tool_calls` | `AVELIS_ANALYSIS_TOOL_CALLS` | `30` | 工具调用次数上限 |
| `query_limit` | `AVELIS_ANALYSIS_QUERY_LIMIT` | `20` | 查询类工具共用的次数额度 |
| `explore_limit` | `AVELIS_ANALYSIS_EXPLORE_LIMIT` | `8` | 探索查询次数上限 |
| `model_requests` | `AVELIS_ANALYSIS_MODEL_REQUESTS` | `16` | 模型请求次数上限（含失败与重试） |
| `input_tokens` | `AVELIS_ANALYSIS_INPUT_TOKENS` | `552321` | 累计输入 token 上限，`0` 为不限 |
| `output_tokens` | `AVELIS_ANALYSIS_OUTPUT_TOKENS` | `30716` | 累计输出 token 上限（含推理），`0` 为不限 |
| `material_change_ratio` | `AVELIS_MATERIAL_CHANGE_RATIO` | `0.01` | 变化显著性阈值（口径未单独设置时） |
| `retrieval` | `AVELIS_ANALYSIS_RETRIEVAL` | `true` | 元数据图证据发现（实验特性）；显式设为 `false` 可关闭 |
| `investigation` | `AVELIS_ANALYSIS_INVESTIGATION` | `true` | 证据解释（实验特性）；显式设为 `false` 可关闭 |
| `uniform_tolerance` | `AVELIS_ANALYSIS_UNIFORM_TOLERANCE` | `0.5` | 均匀容差，有限数值，范围 `(0, 1]`；至少三个正基数成员的变化率都在总体变化率的 ±此比例以内，才判为均匀 |
| `spike_share` | `AVELIS_ANALYSIS_SPIKE_SHARE` | `0.5` | 时间集中度阈值，有限数值，范围 `(0, 1]`；最大单日绝对偏差占总偏差绝对值达到此比例，判为集中在某个时间点 |

这两项实验特性默认开启；未配置模型或没有可用元数据图时，直接跳过额外证据发现与解释，不影响原有问数与分析流程。

### 智能体 `[agent]`

`reply_language`（环境变量 `AVELIS_REPLY_LANGUAGE`）默认是 `"auto"`，分析与问数的回答跟随请求的 `Accept-Language`；也可固定为 `"zh-CN"`、`"zh-TW"` 或 `"en"`。固定回答语言只影响智能体生成给用户看的文字，接口提示与导出标签仍按请求语言返回。

分析运行把请求语言保存在已有的检查点 JSON 中；手动“立即运行”任务时，语言还会写入 Job，独立 worker 领取后仍使用本次请求的语言。问数会话把本轮语言保存在 LangGraph 会话上下文中，后续请求切换界面语言时会更新。无请求上下文的新定时运行默认使用 `zh-CN`，也可用固定回答语言配置控制。指标名、维度成员、字段名、数据取值及引用的元数据保留原文，核验与引用规则不变；语言指令放在运行的用户消息中，系统提示保持稳定。

### 模型 `[model]`

模型提供方与密钥建议在系统的“设置 → 模型配置”页面维护（密钥加密存入元数据库，按用途指定模型）。下面的 `provider` 等几项只用于 `python -m avelis.bootstrap model` 预置一个提供方。

| 配置文件 | 环境变量 | 默认值 | 说明 |
|---|---|---|---|
| `timeout_seconds` | `AVELIS_MODEL_TIMEOUT_SECONDS` | `60` | 分析智能体单次模型请求超时，秒 |
| `provider` | `AVELIS_MODEL_PROVIDER` | 空 | `openai`（含任何 OpenAI 兼容接口）/ `anthropic` |
| `name` | `AVELIS_MODEL_NAME` | 空 | 模型名 |
| `base_url` | `AVELIS_MODEL_BASE_URL` | 空 | 接口地址 |
| `api_key` | `AVELIS_MODEL_API_KEY` | 空 | 密钥 |
| `send_results` | `AVELIS_MODEL_SEND_RESULTS` | `false` | 预置时是否允许把查询结果发给模型 |

### 只能用环境变量设置的项

| 环境变量 | 说明 |
|---|---|
| `AVELIS_CONFIG` | 配置文件路径 |
| `AVELIS_ENV_FILE` | `.env` 文件路径 |
| `AVELIS_PDF_FONT` | 分析报告导出 PDF 时嵌入的中文字体文件（镜像内已装文泉驿微米黑，一般不需要） |
| `AVELIS_PG_PORT`、`AVELIS_PG_PASSWORD` | 本地演示元数据库（未配置 `[database]` 时使用） |
| `AVELIS_DEMO_USER_PASSWORD`、`AVELIS_ADMIN_PASSWORD` | 本地演示账号与本地超级管理员（只用于开发，见 [development.md](development.md)） |

## Docker 部署的编排参数

`deploy/docker/.env`（可选，模板 `.env.example`）只放 Compose 自身的参数，不放应用配置：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AVELIS_HTTP_PORT` | `8080` | 对外端口 |
| `AVELIS_BIND_ADDRESS` | `0.0.0.0` | 绑定地址；前面有反向代理时改为 `127.0.0.1` |
| `AVELIS_IMAGE` | `avelis:1.0.1` | 镜像名 |
| `COMPOSE_FILE` | — | 使用外部 PostgreSQL / Redis 时设为 `compose.yaml:compose.external.yaml` |
