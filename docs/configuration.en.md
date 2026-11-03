# Configuration Reference

English | [简体中文](configuration.md) | [繁體中文](configuration.zh-TW.md)

Avelis can be configured with a **configuration file** (TOML), with **`AVELIS_*` environment variables**, or with a **`.env` file**. Use the configuration file for production deployments; local development keeps using the `.env` file at the repository root.

## Load order

When a setting appears in several places, the value is taken in this order (highest first):

1. Process environment variables `AVELIS_*`
2. The `.env` file: by default `.env` at the repository root; override with `AVELIS_ENV_FILE` (relative paths are resolved against the repository root)
3. The configuration file: the file named by `AVELIS_CONFIG`; if unset, `/etc/avelis/avelis.toml` is read when it exists
4. Built-in defaults

- If `AVELIS_CONFIG` is set but the file is missing or cannot be parsed, start-up fails with the reason.
- Unknown sections or keys in the configuration file (usually typos) are also errors, and the message suggests the closest valid key.
- The Docker image expects the configuration file at `/etc/avelis/avelis.toml` inside the container; the Compose files already set `AVELIS_CONFIG`.
- Sample file: [deploy/docker/avelis.example.toml](../deploy/docker/avelis.example.toml) (every setting has a comment, in Chinese, and its default).

### Check the configuration

```bash
cd backend && uv run python -m avelis.config check                 # local
docker compose run --rm app python -m avelis.config check          # Docker deployment (run in deploy/docker)
```

The output lists each setting's effective value and source (environment variable / .env / configuration file / default); passwords and keys are shown as `******`. The same validation runs at start-up: a missing required setting or a wrong format prints an explanation (which setting, where its current value comes from, how to fix it) and exits with code 2, without a stack trace.

## Sensitive settings

Passwords and keys may be written in plain text in the configuration file, but:

- Restrict read access to the configuration file and `.env` (for example `chmod 640`, readable by the group of the container user, UID 10001). Never commit them to a repository or paste them into chats or tickets.
- Back up `AVELIS_SECRET` together with the metadata database: without it, saved data source passwords and model keys cannot be decrypted.
- Prefer **file references**: put the password in a separate file (for example Docker or Kubernetes secrets) and write only its path in the configuration.

| Form | Example |
|---|---|
| Plain text in the configuration file | `[database]` `password = "…"` |
| File reference in the configuration file | `[database]` `password_file = "/run/secrets/avelis_db_password"` |
| Plain text environment variable | `AVELIS_DB_PASSWORD=…` |
| File reference environment variable | `AVELIS_DB_PASSWORD_FILE=/run/secrets/avelis_db_password` |

Settings that accept file references: `AVELIS_SECRET`, `AVELIS_DB_PASSWORD`, `AVELIS_REDIS_PASSWORD`, `AVELIS_INITIAL_ADMIN_PASSWORD`, `AVELIS_MODEL_API_KEY`, `AVELIS_DATABASE_URL`, `AVELIS_REDIS_URL`. Giving both plain text and a file reference in the same layer is an error; across layers the precedence in [Load order](#load-order) applies (for example, the environment variable `AVELIS_DB_PASSWORD_FILE` overrides a plain-text password in the configuration file).

## Settings

The “Configuration file” column is `[section] key` in TOML. “Required” means there is no usable default and the operator must provide a value.

### Security `[security]`

| Configuration file | Environment variable | Default | Description |
|---|---|---|---|
| `secret` | `AVELIS_SECRET` | — (**required**) | Master key, at least 16 characters; encrypts data source passwords and model keys and signs sign-in tokens. Do not change it after going live |
| `access_token_minutes` | `AVELIS_ACCESS_TOKEN_MINUTES` | `120` | Lifetime of one sign-in token (idle timeout), minutes |
| `session_max_hours` | `AVELIS_SESSION_MAX_HOURS` | `168` | Maximum total length of a sign-in session including renewals, hours |
| `login_max_failures` | `AVELIS_LOGIN_MAX_FAILURES` | `5` | Consecutive failures per account + client address before locking |
| `login_lock_minutes` | `AVELIS_LOGIN_LOCK_MINUTES` | `15` | Lock duration, minutes |
| `allow_self_review` | `AVELIS_ALLOW_SELF_REVIEW` | `false` | Whether submitters may review their own changes |

### Metadata database `[database]`

Only PostgreSQL is supported. Choose one way to connect, in this order of precedence: full connection string `url` → individual fields (when `host` is set) → local demo default (`127.0.0.1` + `AVELIS_PG_PORT` / `AVELIS_PG_PASSWORD`, development only).

| Configuration file | Environment variable | Default | Description |
|---|---|---|---|
| `url` | `AVELIS_DATABASE_URL` | empty | Full connection string `postgresql+psycopg://user:password@host:port/dbname`; the individual fields are ignored when set |
| `host` | `AVELIS_DB_HOST` | empty | Host; the built-in Docker service is `postgres` |
| `port` | `AVELIS_DB_PORT` | `5432` | Port |
| `name` | `AVELIS_DB_NAME` | `avelis` | Database name |
| `user` | `AVELIS_DB_USER` | `avelis` | Account (must be able to create tables and schemas in the database) |
| `password` | `AVELIS_DB_PASSWORD` | — (**required** with individual fields) | Password |
| `sslmode` | `AVELIS_DB_SSLMODE` | empty (driver default `prefer`) | `disable` / `allow` / `prefer` / `require` / `verify-ca` / `verify-full` |
| `pool_size` | `AVELIS_DB_POOL_SIZE` | `5` | Connection pool size per process |
| `max_overflow` | `AVELIS_DB_MAX_OVERFLOW` | `10` | Temporary pool overflow limit |
| `pool_recycle_seconds` | `AVELIS_DB_POOL_RECYCLE_SECONDS` | `1800` | Connection recycle period, seconds |

### Redis `[redis]`

Any Redis-protocol service (Redis or Valkey). Precedence: `url` → individual fields (when `host` is set) → local demo default (`127.0.0.1` + `AVELIS_REDIS_PORT` / `AVELIS_REDIS_PASSWORD`).

| Configuration file | Environment variable | Default | Description |
|---|---|---|---|
| `url` | `AVELIS_REDIS_URL` | empty | Full connection string `redis://:password@host:port/0` |
| `host` | `AVELIS_REDIS_HOST` | empty | Host; the built-in Docker service is `redis` |
| `port` | `AVELIS_REDIS_PORT` | `6379` (`16379` as the demo default when `host` is empty) | Port |
| `db` | `AVELIS_REDIS_DB` | `0` | Database number |
| `password` | `AVELIS_REDIS_PASSWORD` | empty | Password (**required** for the built-in Docker service) |
| `ssl` | `AVELIS_REDIS_SSL` | `false` | Use TLS (`rediss://`) |

### Server `[server]`

| Configuration file | Environment variable | Default | Description |
|---|---|---|---|
| `host` | `AVELIS_HOST` | `127.0.0.1` (`0.0.0.0` in the image) | Listen address |
| `port` | `AVELIS_PORT` | `8000` | Listen port (the published Docker port is set in Compose) |
| `workers` | `AVELIS_WORKERS` | `1` | Number of processes (uvicorn workers) |
| `public_url` | `AVELIS_PUBLIC_URL` | empty (**required** when MCP is enabled) | Address users open, such as `http://10.0.0.5:8080`; shown in the start-up log and added to the CORS allow list; also serves as the OAuth issuer when MCP is enabled, as described below |
| `frontend_dir` | `AVELIS_FRONTEND_DIR` | empty (`/app/frontend/dist` in the image) | Directory of the built frontend; when set, the same port serves the frontend and SPA routes fall back to `index.html`. Relative paths are resolved against the repository root |
| `forwarded_allow_ips` | `AVELIS_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies whose `X-Forwarded-For` is trusted (comma-separated, `*` for all) |
| `log_level` | `AVELIS_LOG_LEVEL` | `info` | `critical` / `error` / `warning` / `info` / `debug` |
| `cors_origins` | `AVELIS_CORS_ORIGINS` | `http://127.0.0.1:5173`, `http://localhost:5173` | CORS allow list (an array in the configuration file, comma-separated in the environment variable); not needed when the frontend is served from the same port |

### MCP service `[mcp]`

MCP runs in the same process as the Avelis backend and is disabled by default. When disabled, `/mcp`, OAuth protocol endpoints, and `/api/oauth/*` management endpoints all return 404, including when frontend SPA hosting is configured; they do not fall back to a frontend page.

| Configuration file | Environment variable | Default | Description |
|---|---|---|---|
| `enabled` | `AVELIS_MCP_ENABLED` | `false` | Enable the MCP service and built-in OAuth authorization server |
| `access_token_seconds` | `AVELIS_MCP_ACCESS_TOKEN_SECONDS` | `900` | OAuth access token lifetime, seconds (15 minutes); must be a positive integer |
| `refresh_token_seconds` | `AVELIS_MCP_REFRESH_TOKEN_SECONDS` | `2592000` | OAuth refresh token lifetime, seconds (30 days); must be a positive integer; each refresh rotates the token |
| `cimd_cache_ttl_seconds` | `AVELIS_MCP_CIMD_CACHE_TTL_SECONDS` | `3600` | CIMD client metadata cache lifetime, seconds; fetch again after expiry; must be a positive integer |
| `cimd_allowed_urls` | `AVELIS_MCP_CIMD_ALLOWED_URLS` | `[]` | Exact allow list of local development CIMD URLs (an array in the configuration file, comma-separated in the environment variable); accepts only complete `http(s)://127.0.0.1:PORT/path` URLs, with no arbitrary private addresses or wildcards |

Before enabling MCP, set `[server] public_url` (`AVELIS_PUBLIC_URL`) to the HTTP(S) base address clients actually use, without credentials, query parameters, or a fragment. The OAuth `issuer` is that exact string; the resource URI is that string with `/mcp` appended, without removing a trailing slash. The `resource` submitted during authorization, code exchange, and refresh must match this resource URI exactly. Use HTTPS in production and ensure the reverse proxy preserves these public paths.

HTTPS is required in production. `public_url` must match the external URL clients use, and the reverse proxy must correctly forward the request scheme and forwarded headers, with trusted proxy addresses configured through `[server] forwarded_allow_ips`. If Authlib rejects insecure transport during local HTTP development or testing, explicitly set `AUTHLIB_INSECURE_TRANSPORT=1`. This is a standard Authlib environment variable for local development and testing only, not an `AVELIS_*` setting; the service does not unconditionally disable the transport check.

Enabling MCP exposes these paths:

- `/mcp`: stateless Streamable HTTP, requiring `Mcp-Protocol-Version: 2026-07-28`.
- `/.well-known/oauth-authorization-server` and `/.well-known/oauth-protected-resource/mcp`: authorization server and protected resource metadata.
- `/oauth/authorize`, `/oauth/token`, `/oauth/revoke`, and `/oauth/register`: authorization, code exchange and refresh, token revocation, and dynamic registration for older clients.
- `/api/oauth/consent/{ticket}` and its `/approve` and `/deny` endpoints: read and confirm consent using an existing Avelis Bearer sign-in token; `/oauth/authorize` sends the client request to the frontend `/oauth/consent?ticket=…` route with a ticket valid for 5 minutes and one use.
- `/api/oauth/authorizations` and `/api/oauth/authorizations/{id}`: view and revoke the current user's grants; `/api/oauth/clients` and `/api/oauth/clients/{id}/revoke`: administrators view and disable clients.

By default, CIMD fetches only HTTPS documents whose resolved addresses are all public, pinning connections to a checked IP. The total timeout is 3 seconds, the document limit is 5 KiB, and redirects, compression, and proxies are rejected. `cimd_allowed_urls` permits HTTP and loopback access only for the listed local development URLs; all other fetch limits still apply. Authorization codes, consent tickets, and tokens are stored only as hashes in the database. Revoking a grant, disabling a client, or detecting authorization code or refresh token replay invalidates the corresponding tokens.

MCP tools use the represented user's existing business domain permissions, restricted by the granted `metrics:read`, `lineage:read`, and `ask:execute` scopes. `ask_metric` uses the same compiler, query timeout, and row limit as the Ask page, returning at most 200 rows by default. It follows the existing data egress setting (`[model] send_results` / `AVELIS_MODEL_SEND_RESULTS`); when disabled, it returns only the metric definition and compiled query explanation, without data rows.

### Bootstrap `[bootstrap]`

| Configuration file | Environment variable | Default | Description |
|---|---|---|---|
| `auto_migrate` | `AVELIS_AUTO_MIGRATE` | `false` (`true` in the Docker sample) | Migrate the metadata database when `avelis.server` starts (serialized by a PostgreSQL advisory lock) |
| `auto_init_admin` | `AVELIS_AUTO_INIT_ADMIN` | `false` (`true` in the Docker sample) | Create the first administrator on start (skipped when an enabled administrator exists) |
| `initial_admin_username` | `AVELIS_INITIAL_ADMIN_USERNAME` | `admin` | First administrator's username |
| `initial_admin_password` | `AVELIS_INITIAL_ADMIN_PASSWORD` | empty (uses `123456`) | Optional; when unset or empty, uses the default password `123456` without a strength check or forced password change. Change it in the UI after signing in. An explicitly supplied password must be 12–128 characters, must not contain the username, and must have at least 4 distinct characters; it must be changed on first sign-in |

With automatic initialization enabled, these rules apply to every deployment, regardless of environment or whether the metadata database is local. The default username is `admin`; use `initial_admin_username` to change it. If an enabled administrator already exists, initialization is skipped without changing any account. An existing non-administrator with the same username causes an error and is left unchanged.

### Business `[business]`

| Configuration file | Environment variable | Default | Description |
|---|---|---|---|
| `timezone` | `AVELIS_BUSINESS_TIMEZONE` | `Asia/Shanghai` | Business time zone |
| `unassigned_asset_policy` | `AVELIS_UNASSIGNED_ASSET_POLICY` | `deny` | Assets without a business domain: `deny` (visible only to administrators and maintainers) / `allow` |
| `require_column_comments` | `AVELIS_REQUIRE_COLUMN_COMMENTS` | `false` | Block publishing when a column used by a metric definition has no database comment (otherwise only warn) |

### Ask and queries `[query]`

| Configuration file | Environment variable | Default | Description |
|---|---|---|---|
| `timeout_seconds` | `AVELIS_QUERY_TIMEOUT_SECONDS` | `30` | Timeout per query, seconds |
| `mem_limit_bytes` | `AVELIS_QUERY_MEM_LIMIT_BYTES` | `2147483648` | Memory limit per query (engines that support it), bytes |
| `max_rows` | `AVELIS_QUERY_MAX_ROWS` | `1000` | Maximum rows returned per query |
| `answer_cache_seconds` | `AVELIS_ANSWER_CACHE_SECONDS` | `300` | Answer cache lifetime, seconds |
| `live_check` | `AVELIS_ASK_LIVE_CHECK` | `true` | Before running an Ask query, check that the source schema still matches the last collection |
| `max_tool_rounds` | `AVELIS_ASK_MAX_TOOL_ROUNDS` | `8` | Maximum tool-call rounds of the Ask agent per answer |

### Collection and background jobs `[collect]` `[worker]` `[history]` `[import]`

| Configuration file | Environment variable | Default | Description |
|---|---|---|---|
| `[collect] interval_minutes` | `AVELIS_COLLECT_INTERVAL_MINUTES` | `15` | Default collection interval for new data sources, minutes (each data source can change it) |
| `[collect] ddl_audit_slack_seconds` | `AVELIS_DDL_AUDIT_SLACK_SECONDS` | `5` | Clock tolerance at both ends of the DDL attribution window, seconds |
| `[worker] schedule_seconds` | `AVELIS_WORKER_SCHEDULE_SECONDS` | `60` | Scheduler scan interval, seconds |
| `[worker] stale_seconds` | `AVELIS_WORKER_STALE_SECONDS` | `30` | Queued jobs not started after this long are picked up by the scan, seconds |
| `[worker] running_timeout_seconds` | `AVELIS_WORKER_RUNNING_TIMEOUT_SECONDS` | `3600` | Jobs running longer are marked failed (not retried), seconds |
| `[history] retention_days` | `AVELIS_HISTORY_RETENTION_DAYS` | `1095` | Change history retention, days; `0` keeps everything |
| `[history] purge_interval_hours` | `AVELIS_HISTORY_PURGE_INTERVAL_HOURS` | `24` | Interval for purging expired history, hours |
| `[history] export_max_rows` | `AVELIS_HISTORY_EXPORT_MAX_ROWS` | `50000` | Maximum rows per change-record export |
| `[import] max_bytes` | `AVELIS_IMPORT_MAX_BYTES` | `2097152` | Size limit for one uploaded file or pasted text when importing lineage, bytes |

Governance finalization for timed-out collection and import jobs is retried by later scans; business work itself is not rerun automatically. Webhook delivery shares a fixed 5-second budget per scan, with at most 5 seconds per request and 3 seconds for connecting. Two consecutive failures skip the subscription's remaining items for that scan. This budget currently has no environment variable or TOML setting; see [delivery rules](events.en.md#delivery-and-signature-verification).

### Analysis agent `[analysis]`

Budget per analysis; when any limit is reached, the agent stops fetching data and finishes the report.

| Configuration file | Environment variable | Default | Description |
|---|---|---|---|
| `seconds` | `AVELIS_ANALYSIS_SECONDS` | `300` | Time limit, seconds (the synchronous analysis endpoint adds 120 seconds to finish) |
| `tool_calls` | `AVELIS_ANALYSIS_TOOL_CALLS` | `30` | Maximum tool calls |
| `query_limit` | `AVELIS_ANALYSIS_QUERY_LIMIT` | `20` | Shared quota for query tools |
| `explore_limit` | `AVELIS_ANALYSIS_EXPLORE_LIMIT` | `8` | Maximum exploratory queries |
| `model_requests` | `AVELIS_ANALYSIS_MODEL_REQUESTS` | `16` | Maximum model requests (including failures and retries) |
| `input_tokens` | `AVELIS_ANALYSIS_INPUT_TOKENS` | `552321` | Cumulative input token limit; `0` for no limit |
| `output_tokens` | `AVELIS_ANALYSIS_OUTPUT_TOKENS` | `30716` | Cumulative output token limit (including reasoning); `0` for no limit |
| `material_change_ratio` | `AVELIS_MATERIAL_CHANGE_RATIO` | `0.01` | Threshold for a material change (when a metric definition does not set its own) |
| `retrieval` | `AVELIS_ANALYSIS_RETRIEVAL` | `false` | Evidence discovery over the metadata graph (experimental) |
| `investigation` | `AVELIS_ANALYSIS_INVESTIGATION` | `false` | Evidence interpretation (experimental; only the switch is defined so far) |

### Model `[model]`

Manage model providers and keys on the **设置 → 模型配置** (Settings → Models) page: keys are stored encrypted in the metadata database and models are assigned per purpose. `provider` and the related settings below are only used by `python -m avelis.bootstrap model` to pre-register one provider.

| Configuration file | Environment variable | Default | Description |
|---|---|---|---|
| `timeout_seconds` | `AVELIS_MODEL_TIMEOUT_SECONDS` | `60` | Timeout of one model request made by the analysis agent, seconds |
| `provider` | `AVELIS_MODEL_PROVIDER` | empty | `openai` (including any OpenAI-compatible endpoint) / `anthropic` |
| `name` | `AVELIS_MODEL_NAME` | empty | Model name |
| `base_url` | `AVELIS_MODEL_BASE_URL` | empty | Endpoint URL |
| `api_key` | `AVELIS_MODEL_API_KEY` | empty | API key |
| `send_results` | `AVELIS_MODEL_SEND_RESULTS` | `false` | Whether the pre-registered provider may receive query results |

### Environment-only settings

| Environment variable | Description |
|---|---|
| `AVELIS_CONFIG` | Path of the configuration file |
| `AVELIS_ENV_FILE` | Path of the `.env` file |
| `AVELIS_PDF_FONT` | CJK font file embedded in exported PDF reports (the image already includes WenQuanYi Micro Hei, so this is rarely needed) |
| `AVELIS_PG_PORT`, `AVELIS_PG_PASSWORD` | Local demo metadata database (used when `[database]` is not configured) |
| `AVELIS_DEMO_USER_PASSWORD`, `AVELIS_ADMIN_PASSWORD` | Local demo accounts and the local superuser (development only; see the [development guide](development.en.md)) |

## Compose parameters for Docker deployments

`deploy/docker/.env` (optional; template `.env.example`) holds only Compose parameters, not application settings:

| Variable | Default | Description |
|---|---|---|
| `AVELIS_HTTP_PORT` | `8080` | Published port |
| `AVELIS_BIND_ADDRESS` | `0.0.0.0` | Bind address; set to `127.0.0.1` behind a reverse proxy |
| `AVELIS_IMAGE` | `avelis:1.0.1` | Image name |
| `COMPOSE_FILE` | — | Set to `compose.yaml:compose.external.yaml` when using an external PostgreSQL / Redis |
