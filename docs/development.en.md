# Development Guide

English | [简体中文](development.md) | [繁體中文](development.zh-TW.md)

This guide is for developers who build, debug and run acceptance tests for Avelis on their own machine: backend and frontend commands, the local demo environment and the one-command acceptance run. To deploy to a server, see the [deployment guide](deployment.en.md); for settings, see the [configuration reference](configuration.en.md).

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (uses Python 3.12 automatically). On Windows, do not call a bare `python`; always use `uv run`.
- Node.js 24 and pnpm 11.
- Docker Desktop / Docker Engine (Compose v2; reserve about 7 GB of memory for the demo containers).

The development environment needs internet access to install dependencies (`uv sync`, `pnpm install`). The deployment Docker image, by contrast, installs everything at build time and downloads nothing at run time; see the [deployment guide](deployment.en.md).

During development the backend and frontend run separately: the backend on `127.0.0.1:8000`, and the Vite dev server on `127.0.0.1:5173`, which proxies `/api` to the backend. In production the backend serves both the API and the frontend on one port; see the [deployment guide](deployment.en.md).

## Backend

```bash
cd backend
uv sync
uv run python -m avelis.bootstrap migrate      # migrate the metadata database (PostgreSQL)
uv run python -m avelis.bootstrap admin        # create the automation admin ops_admin and the local superuser admin (see below)
uv run uvicorn avelis.api.app:app --port 8000  # API; docs at http://127.0.0.1:8000/api/docs
uv run python -m avelis.jobs                   # background worker: collection jobs, scheduling
uv run python -m avelis.config check           # show the effective configuration and sources (passwords masked)
AVELIS_FRONTEND_DIR=frontend/dist uv run python -m avelis.server  # run on one port like production (run pnpm build first)
uv run pytest                                  # unit tests
uvx ruff@0.17.3 check .                        # lint (rules in pyproject.toml; run again at the repository root for scripts and experiments)
uv run pytest -m acceptance                    # acceptance tests (reset the demo databases and the metadata database)
uv run python -m avelis.bootstrap demo --reset # demo walkthrough: reset, then create users, business domains and data sources via the API and run the first collection
```

- **Authentication**: OAuth2 password flow + PyJWT access tokens (revocable in Redis); passwords hashed with pwdlib (Argon2); data source passwords encrypted with Fernet and never returned by the API. Failed sign-ins are rate-limited per account and client address (`AVELIS_LOGIN_MAX_FAILURES` / `AVELIS_LOGIN_LOCK_MINUTES`). Accounts whose password was created or reset by an administrator must change it on first sign-in; changing, resetting or disabling invalidates existing sessions at once.
- **Roles**: administrator, maintainer, reviewer and querier, plus business-domain membership (owner / maintainer / can query). Self-review is forbidden by default; see `/api/settings` for the policy.
- **Ask**: a LangGraph agent that asks follow-up questions and understands them in context; session state lives in PostgreSQL. The model only submits a structured intent; SQL is generated deterministically from published metric definitions and validated. It supports several metrics, ranking and top N, period-over-period and year-over-year, and derived metrics (for example average order value = paid sales / order count); see [ADR 0005](decisions/0005-ask-expressiveness.md) (Simplified Chinese). Without a configured model, a rule-based demo model is used and the answer says so.
- **Analysis**: a multi-step analysis agent for questions like “why did this change, what is related, what should we do”.
  - Data is fetched only through governed queries, and computation happens in the backend;
  - every number in the report is verified, and conclusions are labelled as fact, calculation, inference or recommendation;
  - reports export to PDF / Markdown / CSV, and analyses can be saved as scheduled tasks.

  It needs a real model with tool calling; see [ADR 0007](decisions/0007-analysis-agent.md) (Simplified Chinese). Load the synthetic e-commerce demo dataset with `uv run scripts/ec_data.py load`; `bootstrap demo` picks it up automatically.
- **Model settings**: managed on the **设置 → 模型配置** (Settings → Models) page. Supports DeepSeek, OpenAI, Anthropic, Qwen and custom OpenAI-compatible endpoints. Keys are stored encrypted and only the last four characters are shown; the connection test checks tool calling; models are assigned per purpose. By default, query results and redacted code snippets are sent to the model (analysis needs them); this can be turned off on the same page, and a local model is recommended when data must stay private. During development, `uv run python -m avelis.bootstrap model` registers the `AVELIS_MODEL_*` values from `.env`.
- **Lineage import**: upload SQL files, paste SQL, or upload Python / Shell code. Parsed edges are written directly (and can be edited); fragments that cannot be parsed become model candidates that a person must accept, edit or reject.
- **Database engines**: each database is an adapter in `avelis.engines` (connector, SQL dialect, asset name levels, capability declaration), and core modules never branch on database type; see `GET /api/engines`. Registered today: StarRocks, PostgreSQL, ClickHouse, SQL Server, Oracle and Doris (collection, view lineage, Ask, pre-query schema check), and MySQL (collection, as the target of external tables). Start the demo databases for the additional engines with `uv run scripts/demo.py up --engines`; per-engine acceptance records are in the [additional engines acceptance (archived, Simplified Chinese)](archive/acceptance/acceptance-m7.md). How to add a database: [ADR 0004](decisions/0004-engine-adapters.md) (Simplified Chinese).
- **Pre-query schema check**: before every Ask execution (including cache hits), the source columns and view definitions are compared with the last collection; on a mismatch the query is blocked and a collection is queued (`AVELIS_ASK_LIVE_CHECK`, on by default).
- **Column meaning**: database column comments are the source of truth; Avelis has no separate display names. Ask and the asset pages show column names and comments as they are and flag missing comments; data source details show comment coverage and a list of gaps (exportable), and the pre-publish check warns about missing comments (blocks when `AVELIS_REQUIRE_COLUMN_COMMENTS=true`); see [ADR 0006](decisions/0006-column-meaning-from-db-comments.md) (Simplified Chinese).
- **Permissions**: assets are visible by business domain; assets without a business domain are visible only to administrators and maintainers by default (`AVELIS_UNASSIGNED_ASSET_POLICY=deny|allow`).
- **Worker**: `AVELIS_WORKER_SCHEDULE_SECONDS` (scan interval, default 60), `AVELIS_WORKER_STALE_SECONDS` (threshold for picking up jobs that were not queued, default 30), `AVELIS_WORKER_RUNNING_TIMEOUT_SECONDS` (jobs running longer are marked failed, default 3600).
- Design decisions: [ADR 0002](decisions/0002-backend-foundations.md), [ADR 0003](decisions/0003-postgres-agent-model-settings.md), [ADR 0004](decisions/0004-engine-adapters.md) (Simplified Chinese).

Unit tests do not connect to any external service and run offline (with a fake key, without reading `.env`):

```bash
cd backend
AVELIS_SECRET=offline-unit-test-secret-0123456789 AVELIS_ENV_FILE=/nonexistent uv run --frozen python -m pytest tests/unit -q -p no:cacheprovider
```

## Frontend

Accounts:
- `admin`: the local superuser with every role; its password is `AVELIS_ADMIN_PASSWORD` in `.env`. It is created only when the metadata database is on this machine and skips the password policy; `bootstrap admin` resets it to the configured value.
- `ops_admin`: the automation administrator used by demo setup and acceptance scripts.
- Demo accounts: zhang (owner of the transactions domain), zhao (owner of the products domain), li (reviewer), wang and sun (analysts).

`ops_admin` and the demo accounts all use `AVELIS_DEMO_USER_PASSWORD` from `.env`.

```bash
cd frontend
pnpm install
pnpm dev            # http://127.0.0.1:5173, proxies /api to 127.0.0.1:8000
pnpm gen:api        # regenerate types after backend API changes
pnpm gen:tokens     # regenerate tokens.css after design token changes
```

All styles come from the Avelis UI design system: `frontend/design/tokens.json` generates `src/styles/tokens.css`, and component styles are in `src/styles/avelis-ui.css`.

## Demo environment

For local development and acceptance only. Every port binds to `127.0.0.1`, and all data is synthetic e-commerce data with fixed dates:

- **StarRocks 3.5.14** (single container): fact tables `shop.orders`, `shop.order_items`, `shop.refund_raw`, the view `shop.daily_sales`, and analysis views `ads.*`.
- **E-commerce dimension database, MySQL 8.4**: customers, products, categories and regions in `shop_dim`. StarRocks reaches it in two ways: the JDBC catalog `shop_mysql` and the legacy `ENGINE=mysql` external table `dim.ext_region`.
- **Avelis metadata database**: PostgreSQL 18 (also stores the Ask agent's session state).
- **Valkey 8** (Redis protocol): job queue, cache, collection locks.

### Resources

- Docker Desktop (reserve about 7 GB of memory for the containers; the StarRocks container is capped at 5.25 GB)
- [uv](https://docs.astral.sh/uv/) (uses Python 3.12 automatically)

### Common commands

```bash
uv sync
uv run scripts/demo.py up        # generate .env, verify the JDBC driver, start, wait until ready, load data and check the baseline
uv run scripts/demo.py check     # check the acceptance baseline again
uv run scripts/demo.py seed      # reset the demo databases to the baseline
uv run scripts/demo.py verify    # StarRocks verification on a real instance; results in .demo/sr-verify.json
uv run scripts/demo.py down      # stop; add --volumes to delete the data too
```

Long-running worker check (temporarily sets the collection interval to 1 minute, restarts Valkey and changes a view once along the way, then restores everything):

```bash
cd backend && uv run python ../scripts/worker_soak.py --minutes 60
```

### Ports and accounts

| Service | Address | Accounts |
|---|---|---|
| StarRocks (MySQL protocol) | `127.0.0.1:19030` | `root` (data loading only), `avelis_collector` (collection), `avelis_reader` (read-only queries) |
| StarRocks FE HTTP | `127.0.0.1:18030` | — |
| E-commerce dimension MySQL | `127.0.0.1:13306` (`shop-mysql:3306` inside the container network) | `avelis_collector` (read-only metadata), `sr_catalog` (JDBC catalog), `sr_legacy` (legacy external table) |
| Metadata database PostgreSQL | `127.0.0.1:15432` | `avelis` |
| Valkey | `127.0.0.1:16379` | password authentication |

All passwords are generated randomly by `demo.py init-env` and written to `.env` (never committed and never printed). The demo network uses the fixed subnet `10.240.17.0/24`; if it conflicts with your network, change `AVELIS_DEMO_SUBNET` in `.env`.

### Acceptance baseline (`demo.py check`, queried with the read-only account)

| Item | Expected |
|---|---|
| `shop.daily_sales` 2026-09-15, East China (paid) | 300.00 |
| Same day and region, net of refunds | 270.00 |
| Orders joined directly to refund lines, summed (grain inflation) | 400.00 |
| Orders joined directly to order lines, summed (grain inflation) | 400.00 |
| `ads.sales_by_category` 2026-09-15, kitchenware (joined to the MySQL dimension table) | 270.00 |
| `ads.member_sales` 2026-09-15, gold (joined to the MySQL dimension table) | 250.00 |
| `ads.region_board` East China 2026-09-15 (through the legacy external table) | 300.00 |
| Rows in the product dimension table via the catalog | 4 |

## One-command acceptance

Prerequisites: Docker (Compose v2, about 7 GB of memory for the containers), [uv](https://docs.astral.sh/uv/), pnpm.

```bash
uv run scripts/acceptance.py --fresh     # from scratch: delete the demo containers and volumes, rebuild, then run every acceptance test
uv run scripts/acceptance.py             # reuse the running demo containers
uv run scripts/acceptance.py --no-llm    # do not call a real model (nothing is sent to a model provider)
```

Steps, in order:

1. Generate `.env` (random passwords, never printed);
2. verify the JDBC driver, start the containers, load the baseline and check the 8 baseline values;
3. migrate the metadata database;
4. run the backend unit tests;
5. run acceptance (stories 1–9 and the failure scenarios, all on real StarRocks / MySQL / PostgreSQL / Valkey);
6. type-check and build the frontend;
7. reset the demo environment to the start of story 1.

The report is written to `.demo/acceptance-report.md`. When `.env` has a model key, 2 real-model cases run and the questions and metric metadata are sent to the configured model provider. Expected values and past results are in the [acceptance summary (archived, Simplified Chinese)](archive/acceptance/acceptance.md).

## Design

- Design tokens: `frontend/design/tokens.json`; `pnpm gen:tokens` generates `frontend/src/styles/tokens.css`; component styles are in `frontend/src/styles/avelis-ui.css`.
- Brand mark: the mark in use is `frontend/public/favicon.svg` and `frontend/src/components/Logo.tsx`; `design/brand/` keeps the earlier mark concepts.
- For how to write documentation, see the [documentation guide](documentation-guide.en.md).
