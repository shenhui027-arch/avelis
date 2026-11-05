<div align="center">

<img src="frontend/public/favicon.svg" alt="" width="72" height="72">

# Avelis

English | [简体中文](README.md) | [繁體中文](README.zh-TW.md)

**Grounded data Q&A and analysis, built on lineage, governed metrics and change management.**

[![CI](https://github.com/shenhui027-arch/avelis/actions/workflows/ci.yml/badge.svg)](https://github.com/shenhui027-arch/avelis/actions/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-Apache--2.0-0A7BBB?style=flat)](LICENSE)
[![backend](https://img.shields.io/badge/backend-Python%203.12%20%C2%B7%20FastAPI-3776AB?style=flat&logo=python&logoColor=white)](backend/pyproject.toml)
[![frontend](https://img.shields.io/badge/frontend-React%2019%20%C2%B7%20TypeScript-2F74C0?style=flat&logo=react&logoColor=white)](frontend/package.json)
[![agent](https://img.shields.io/badge/agent-LangGraph-1C3C3C?style=flat&logo=langchain&logoColor=white)](docs/architecture.en.md)
[![SQL](https://img.shields.io/badge/SQL-SQLGlot%20%C2%B7%208%20engines-6E56CF?style=flat)](#supported-databases)
[![deploy](https://img.shields.io/badge/deploy-Docker%20%C2%B7%20Compose-2496ED?style=flat&logo=docker&logoColor=white)](docs/deployment.en.md)
[![version](https://img.shields.io/badge/version-v1.0.7-2EA44F?style=flat)](https://github.com/shenhui027-arch/avelis/releases/tag/v1.0.7)

[Highlights](#highlights) · [What's new](#whats-new) · [Screenshots](#screenshots) · [Databases](#supported-databases) · [Quick start](#quick-start) · [Docs](#documentation) · [Roadmap](#roadmap) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Changelog](CHANGELOG.md)

</div>

Avelis answers business questions about your databases and shows where every number comes from. Each answer carries its source tables, metric definition, version and the permission it was checked against. When an upstream table or view changes, Avelis knows which metrics and answers are affected and blocks the affected metrics in the backend until someone reviews them.

![Analysis session: conclusion-first report, ranked causes, evidence panel and model bar](docs/images/en/analysis.png)

> [!NOTE]
> Avelis v1.0.7 is released; see the [changelog](CHANGELOG.md). The user interface and the documentation are available in English, Simplified Chinese and Traditional Chinese.

## A note from the author

Avelis is open source under the [Apache License 2.0](LICENSE). You are welcome to use it in commercial products, modify it and build on it. There is no need to ask me and nothing to pay; just keep the copyright and license notices the license requires.

If it helps you, I don't want anything in return. If you would still like to say thanks, please consider donating a nutritious school lunch to children in rural or low-income areas, for example through Alipay's charity platform.

AI has let developers like us be among the first to taste the rewards of this wave of progress. I hope some of that good fortune can find another way to reach ordinary children who don't yet have access to AI.

## Highlights

- **Lineage from the database itself.** Collect metadata with a read-only account, identify objects by physical ID, and parse views, external tables and imported SQL / Python / Shell into table- and column-level lineage. Fragments the parser cannot handle become model-suggested candidates that a person confirms.
- **Governed metrics.** Metrics have versions, a publishing state machine, pre-publish checks, review and cross-domain approval. Join paths and grain are checked so joins cannot silently inflate totals.
- **Change management.** Upstream schema change → impact analysis → review. Changes are attributed to the database user who made them, and history is append-only, sealed in a hash chain, searchable and exportable.
- **Grounded Q&A.** A LangGraph agent understands the question and asks follow-ups. The model only submits a structured intent; SQL is generated deterministically from published metrics, validated, and run with a read-only account after checking that the source schema still matches.
- **Analysis agent.** Multi-step analysis of "why did this change, what is related, what should we do". Data is fetched only through governed queries, every number in the report is verified, and conclusions are labelled as fact, calculation, inference or recommendation. Reports export to PDF, HTML or Markdown; analyses can be scheduled.
- **Learns from confirmation.** Questions the model raises are confirmed with the user and stored as governed experience that later questions and analyses reuse.
- **Pluggable models.** Configure DeepSeek, OpenAI, Anthropic, Qwen or any OpenAI-compatible endpoint in the UI and assign models per purpose. Without a model, a rule-based demo model is used and labelled as such.

## What's new

| Version | Added |
|---|---|
| **v1.0.7** | Login page fix: the header and content now share one centered width, so on wide screens the headline, product preview and sign-in card stay together instead of drifting to opposite edges |
| **v1.0.6** | Interface redesign: a soft workbench look (light first, dark just as finished) without gradients, glows or redundant helper text, with extra context behind ⓘ tooltips; Motion animations for sliding selections, page changes and popovers opening and closing; reworked Ask home and workbench; fonts bundled with the image instead of downloaded at runtime |
| **v1.0.5** | Filter value alignment: values such as “East Region” are matched deterministically to dimension members or to the business's own dimension tables (aliases are kept in those tables and registered through join paths); when nothing matches, Avelis asks with clickable candidates. Asset search supports combined keywords and “Load more”; concurrent webhook delivery no longer miscounts retries at the end of a round |
| **v1.0.4** | Edit charts in conversation: say "make it a pie chart", "mark the highest point" or "highlight East" in a follow-up to redraw the chart without querying again; charts mark extremes, averages and highlights, and exports follow; webhooks are delivered concurrently and the outbox is cleaned up on a retention schedule; asset pickers and catalog search query on demand, so large catalogs are no longer loaded in full |
| **v1.0.3** | Chart upgrade: answers to questions now come with charts (trend, ranking, composition, comparison, KPI cards, small multiples), with alternative charts, a table view and PNG download; analysis reports embed charts in the text, and PDF / HTML exports draw from the same chart spec; numbers are formatted by measure type (percentages, thousands / millions, units); the model can pick a chart through the chart tool and give it a takeaway title |
| **v1.0.2** | MCP server: AI tools such as Claude and Cursor query governed metrics and lineage after OAuth authorization; governance events and webhooks: 14 event types such as metric publishing, collection failures and schema changes are pushed to subscribed endpoints; two-way exchange of metrics with Apache Ossie semantic models; metric hints for models (when to use, synonyms, example questions); works out of the box, with a default administrator admin / 123456 and password change on the sign-in page |
| **v1.0.1** | Full MySQL engine (view column lineage, read-only queries); DDL changes on MySQL, SQL Server, Oracle and StarRocks attributed to the database user |
| **v1.0.0** | First public release: metadata collection and column-level lineage, metric governance and change management, grounded natural-language queries, analysis agent |

See the [changelog](CHANGELOG.md) for every change and [Releases](https://github.com/shenhui027-arch/avelis/releases) for downloads.

## Screenshots

| Dataset view | Workbench |
|---|---|
| ![Dataset opened in the main area: chart, rows, rerun with a different time window](docs/images/en/analysis-dataset.png) | ![Workbench: collection, lineage, changes, metrics and Q&A at a glance](docs/images/en/workbench.png) |
| **Lineage** | **Catalog** |
| ![Lineage graph with a selected table and the inspector](docs/images/en/lineage.png) | ![Metric detail: versions, expression, dimensions and pre-publish checks](docs/images/en/catalog.png) |

More pages (Q&A home, report follow-ups, export, personal experience, settings, light theme) are in the [product tour](docs/product-tour.en.md).

## Supported databases

Each database is an engine adapter. Versions are the ones Avelis has been tested against in the local demo environment.

| Database | Tested version | Metadata | View lineage | Q&A queries | Pre-query schema check | DDL attribution |
|---|---|:---:|:---:|:---:|:---:|:---:|
| StarRocks | 3.5.14 + AuditLoader 4.2.2 | ✓ | ✓ | ✓ | ✓ | [✓ (live-verified 2026-11-01; audit table required)](docs/engines/starrocks.md#ddl-变更归因auditloader) |
| PostgreSQL | 18 | ✓ | ✓ | ✓ | ✓ | ✓ |
| ClickHouse | 26.8 | ✓ | ✓ | ✓ | ✓ | ✓ |
| SQL Server | 2022 | ✓ | ✓ | ✓ | ✓ | ✓ (live-verified 2026-11-01; default trace required) |
| Oracle | 23ai Free | ✓ | ✓ | ✓ | ✓ | ✓ (live-verified 2026-11-01; unified audit policy required) |
| Apache Doris | 4.1 | ✓ | ✓ | ✓ | ✓ | ✓ |
| Apache Hive | 4.1 | ✓ | ✓ | | | |
| MySQL | 8.4.10 | ✓ | ✓ | ✓ | ✓ | ✓ (live-verified 2026-11-01; audit table required) |

StarRocks also resolves external tables and JDBC catalogs to the MySQL tables behind them. MySQL supports column-level view lineage, read-only Q&A queries and pre-query schema checks. DDL attribution requires a bound audit table, such as `mysql.general_log` with table output enabled. Live testing confirmed that this log also records failed DDL, so its records are candidate evidence and must agree with collected schema changes. For both MySQL and StarRocks, leaving the audit table unbound produces `unsupported` attribution with a reason stating that no audit table is bound.

The DDL validation on 2026-11-01 has these limits: the SQL Server demo instance had a time zone offset of 0, so local-time conversion to UTC and daylight-saving transitions were not live-tested. Oracle used a custom unified audit policy covering CREATE / ALTER / DROP TABLE, CREATE / DROP VIEW, COMMENT, RENAME and TRUNCATE TABLE, with `AUDIT_VIEWER`; the `not_enabled` branch for no records and no policy was not live-tested. The StarRocks FE used UTC, so non-UTC conversion was not live-tested. Statements executed within the first few tens of seconds after AuditLoader installation may be absent from the audit table and remain unmatched. Only engines supported by [SQLGlot](https://github.com/tobymao/sqlglot) are added; see the [engine validation records](docs/engines/README.md) and [ADR 0004](docs/decisions/0004-engine-adapters.md) (Simplified Chinese).

## Quick start

Requirements: Docker Engine 24+ with Compose v2.24+ (or Docker Desktop).

```bash
git clone https://github.com/shenhui027-arch/avelis.git
cd avelis/deploy/docker
cp avelis.example.toml avelis.toml
```

Edit `avelis.toml` and fill in the three required values: `[security] secret`, `[database] password` and `[redis] password`. The initial administrator password can be left empty. Then start the stack:

```bash
docker compose up -d --build
docker compose ps
```

When `app` shows `healthy`, open `http://<server-ip>:8080`. If the initial password is unset or empty, sign in with the default `admin` / `123456`. A password change is not forced; change it in the UI after signing in. If an initial password was explicitly configured, sign in with it and change it when prompted.

The stack runs the app (API + web UI), a worker, PostgreSQL and Redis (Valkey), with data in Docker volumes. Next steps: add a data source under **设置 → 数据源** (Settings → Data sources) and configure a model under **设置 → 模型配置** (Settings → Models). For external databases, reverse proxy and HTTPS, running without Docker, upgrades and backups, see the [deployment guide](docs/deployment.en.md).

## Architecture

```mermaid
flowchart LR
  UI[Browser] -->|one port| APP
  subgraph Avelis
    APP[app: FastAPI /api<br/>+ React SPA]
    W[worker: collection, schedules]
  end
  APP --> PG[(PostgreSQL metadata DB)]
  W --> PG
  APP --> RD[(Redis / Valkey)]
  W --> RD
  APP -->|read-only account| DB[(Governed databases)]
  W -->|collector account| DB
  APP -->|questions, metadata, results (can be turned off)| LLM[[Model provider]]
```

- **Backend** (`backend/`): Python 3.12, FastAPI, SQLAlchemy + Alembic, SQLGlot, LangGraph.
- **Frontend** (`frontend/`): React, Vite and TypeScript. It talks to the backend only through `/api`, with types generated from OpenAPI.
- **Storage**: PostgreSQL is the only supported metadata database. Redis is used for job wake-ups, caching, locks and token revocation.
- **Deployment**: in production the backend also serves the built frontend, so a single port is enough. The worker is a separate process from the same image.

See the [architecture overview](docs/architecture.en.md) and the [architecture decision records](docs/decisions/) (Simplified Chinese).

## Configuration

A deployment is configured by one commented TOML file, [`deploy/docker/avelis.example.toml`](deploy/docker/avelis.example.toml), copied to `avelis.toml`. Required values:

| Setting | Description |
|---|---|
| `[security] secret` | Master key (at least 16 characters). Encrypts data source passwords and model keys and signs sign-in tokens. Do not change it after going live. |
| `[database] password` | Password of the PostgreSQL metadata database. For an external database, also set host, port, name and user. |
| `[redis] password` | Redis password. |

`[bootstrap] initial_admin_password` is optional. Leave it empty to use the default password `123456`, without a strength check or forced password change; change it in the UI after signing in. An explicitly supplied password must be 12–128 characters, must not contain the username, and must have at least 4 distinct characters; it must be changed on first sign-in. The default username is `admin`; use `initial_admin_username` to change it. If an enabled administrator already exists, initialization is skipped without changing any account.

Automatic migration and first-administrator initialization are enabled by default, even with no `[bootstrap]` section. MCP is also enabled by default, with **Settings → MCP access** ready to use. Without `AVELIS_PUBLIC_URL`, public addresses are derived from the current request's scheme, Host, and ASGI `root_path`. Behind a reverse proxy, configure `AVELIS_FORWARDED_ALLOW_IPS` correctly and preserve Host, or set `AVELIS_PUBLIC_URL` directly. Set `AVELIS_MCP_ENABLED=false` to disable MCP. Metadata graph evidence discovery and evidence interpretation for analysis are also enabled by default (still experimental); extra evidence is skipped when no model or metadata graph is available. Disable them individually with `AVELIS_ANALYSIS_RETRIEVAL=false` and `AVELIS_ANALYSIS_INVESTIGATION=false`.

Precedence: `AVELIS_*` environment variables > `.env` file > configuration file > built-in defaults. Passwords can also be read from files (`password_file` / `AVELIS_*_FILE`). The configuration is validated at startup and errors name the setting to fix. All settings are listed in the [configuration reference](docs/configuration.en.md).

## Development

```bash
# Backend: http://127.0.0.1:8000, API docs at /api/docs
cd backend
uv sync
uv run python -m avelis.bootstrap migrate
uv run uvicorn avelis.api.app:app --port 8000
uv run python -m avelis.jobs          # worker, in another terminal

# Frontend: http://127.0.0.1:5173, proxies /api to port 8000
cd frontend
pnpm install
pnpm dev
```

The [development guide](docs/development.en.md) covers the local demo environment (StarRocks, MySQL, PostgreSQL, Valkey and synthetic data), demo accounts, unit tests and the one-command acceptance run.

## Documentation

| For | Documents |
|---|---|
| Users | [Product tour](docs/product-tour.en.md) |
| Operators | [Deployment guide](docs/deployment.en.md) · [Configuration reference](docs/configuration.en.md) |
| Developers | [Development guide](docs/development.en.md) · [Architecture overview](docs/architecture.en.md) · [Documentation guide](docs/documentation-guide.en.md) |
| Design decisions | [Architecture decision records](docs/decisions/) |

The full index is in [docs/README.en.md](docs/README.en.md). Design decision records (ADRs) are in Simplified Chinese only.

## Roadmap

Planned directions, without dates:

- External catalogs and materialized views for more engines.
- Richer Q&A: nested derived metrics, week and month grains, share of total.

Changes are recorded in the [changelog](CHANGELOG.md).

## Contributing

Contributions are welcome. Please read the [contributing guide](CONTRIBUTING.md) and follow the [code of conduct](CODE_OF_CONDUCT.md). To report a security vulnerability, see the [security policy](SECURITY.md) instead of opening a public issue.

## License

Avelis is licensed under the [Apache License 2.0](LICENSE).
