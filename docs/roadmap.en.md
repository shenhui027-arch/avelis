# Roadmap

English | [简体中文](roadmap.md) | [繁體中文](roadmap.zh-TW.md)

This roadmap is for Avelis developers and contributors. It records the agreed five-phase development plan (`M1` through `M5`) built on the `v1.0.11` baseline, together with its strict execution order, engineering boundaries and acceptance criteria. Every item in `M1` through `M5` is currently **Planned**, with no target dates; for implemented capabilities and system internals, see [Architecture and governance](architecture.en.md) and the [Development guide](development.en.md).

## Execution order and hard boundaries

### Strict execution order

The five phases must be delivered strictly in order: `M1 → M2 → M3 → M4 → M5`. **`M5` starts only after `M1` through `M4` are all completed and verified**:

1. **M1 (Auth security baseline and commercial login redesign, Planned)**: Close authentication, session and authorization vulnerabilities first, and rebuild the sign-in, password-change and admin user-operations UI.
2. **M2 (UI/UX, motion, accessibility and frontend performance, Planned)**: Build on the hardened auth shell to fix broken navigation and retry flows, complete CRUD loops, meet WCAG AA contrast and keyboard accessibility, and split routes and locales.
3. **M3 (Agent intelligence, evaluation and MCP / Skills, Planned)**: Layer hybrid retrieval, domain-shared experiences, a verified query repository, multi-provider SSE streaming with a cost ledger, evaluation uplift and MCP / Skill security on top of the complete UI.
4. **M4 (Data security compliance, observability and CI, Planned)**: Add transport security and read-only session controls across all eight connectors, dual-key rotation, PII masking, row-level security, audit verification UI, structured observability and database-backed CI.
5. **M5 (10k-table scale and multi-replica HA, Planned, final phase)**: Implemented only after `M1` through `M4` pass verification; replaces the global hash-chain row lock, adds incremental 10k-table collection and search indexes, scales workers and cross-replica SSE, adds object storage and delivers cloud-native zero-downtime deployment.

### Hard boundaries and compliance constraints

| Constraint | Requirement |
|---|---|
| Original implementation and license hygiene | Never copy source code lines from existing open-source projects; keep implementations original. Parsing or design ideas may be referenced, and commercially permissive prebuilt libraries or frameworks (MIT, Apache-2.0, BSD, ISC, etc.) may be used with zero commercial licensing risk |
| Explicit non-goals | Do not integrate DingTalk, Feishu or WeCom private SDKs; do not implement SAML 2.0, direct LDAP / AD bind, SCIM 2.0, self-hosted SMTP password-reset emails, or public-cloud self-registration and billing |
| Baseline vs. planned separation | Current `v1.0.11` behavior (for example, `admin / 123456` fallback when no initial password is set, a hard-locked MCP protocol version, the global `history_chain_head` lock, local skill storage and Docker Compose deployment) is replaced incrementally by each planned phase rather than confused with shipped behavior |

## Phase overview

| Phase | Focus | Status | Dependencies | Core scope |
|---|---|---|---|---|
| M1 | Auth security baseline and commercial login redesign | Planned | `v1.0.11` baseline | Fix auth and IDOR vulnerabilities, migrate to Cookie auth, deliver standard OIDC / optional Forward Auth / local TOTP 2FA / PAT, redesign light/dark sign-in and password-change pages, and complete admin user ops |
| M2 | UI/UX, motion, accessibility and frontend performance | Planned | M1 | Fix cross-page parameter loss, retry loops and scheduled-task / data-source CRUD, bring all semantic tokens to WCAG AA, fix overlay/keyboard A11y, streamline motion and cut main bundle size by ≥ 50% |
| M3 | Agent intelligence, evaluation and MCP / Skills | Planned | M2 | Deliver hybrid retrieval, domain-reviewed experiences and VQR, decouple model providers for SSE streaming and cost quotas, lift `scripts/eval_analysis.py` pass rate to ≥ 70%, and harden the sandbox, MCP negotiation and Skill review |
| M4 | Data security compliance, observability and CI | Planned | M3 | Add TLS / CA and read-only sessions across all 8 connectors, `MultiFernet` key rotation, PII masking, RLS, audit & hash-chain UI, JSON logs, `/metrics`, Postgres + Valkey CI and `sqlglot` convergence |
| M5 | 10k-table scale and multi-replica HA | Planned (final phase) | M1–M4 completed and verified | Remove the global row lock for batch or sharded hash chains with streaming cursor purge, add incremental 10k-table collection with `pg_trgm` / GIN indexes, multi-slot workers, cross-replica SSE Pub/Sub, object storage and Helm zero-downtime ops |

## M1: Auth security baseline and commercial login redesign (Planned)

In the `v1.0.11` baseline, leaving `initial_admin_password` unset falls back to `admin / 123456` without forcing a password change, and some endpoints lack state or ownership checks. `M1` plans the following work:

### Frontend sign-in, password-change and admin user operations (Planned)

- **Visual redesign of sign-in and password-change pages**: Rebuild `frontend/src/pages/Login.tsx`, `frontend/src/styles/login.css` and `frontend/src/pages/account/ChangePassword.tsx`. Give the left stage stronger architectural depth and data storytelling with an interactive linked preview (“lineage → metric check → attribution”) tuned for both light and dark themes; refine form layout and focus micro-interactions on the right, fix low contrast on the empty-state primary submit button, and unify the design language across sign-in and password change.
- **Commercial sign-in capabilities**: Dynamic single-slot enterprise OIDC button (clean local-only form when unconfigured, optional toggle to disable local sign-in); light white-label branding (`brand_name`, `brand_logo_url`, `login_subtitle`, `terms_url`, `privacy_url`); 7-day “Remember me”; unified password visibility toggles; a live password-rule checklist; and clear error and lockout countdown feedback.
- **Admin user operations**: Add user search, one-time copyable temporary password reset modal, force-logout and `create.isPending` double-submit protection in `frontend/src/pages/settings/UsersPage.tsx`; add a remove-member button in `frontend/src/pages/catalog/DomainDetail.tsx`; hide unauthorized settings tabs by role in `frontend/src/layout/Shell.tsx`.

### Backend security hardening and lightweight standard SSO (Planned)

- **Password-change enforcement and IDOR fixes**: Enforce `user.must_change_password` in `backend/src/avelis/api/deps.py` (allowlisting only `/api/me`, `/api/me/password` and `/api/auth/logout`), closing the `/oauth/consent` bypass; eliminate silent `admin / 123456` in `backend/src/avelis/bootstrap.py` (require password change on first sign-in or log a generated one-time random password at startup); fix the `GET /api/jobs/{job_id}` IDOR in `backend/src/avelis/api/datasources.py`; add `DELETE /api/domains/{domain_id}/members/{user_id}` in `backend/src/avelis/api/catalog.py`.
- **HTTP security headers and production API docs toggle**: Add HTTP security headers middleware (CSP, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`, conditional `HSTS`) in `backend/src/avelis/api/app.py`, and disable `/api/docs` and `/api/openapi.json` by default in production.
- **Anti-brute-force, Redis resilience and Cookie auth**: Add atomic Redis `MULTI/EXEC` counters, dual account and IP lockout, constant-time Argon2 timing-attack defense, lowercase username normalization and `/api/me/password` rate limiting in `backend/src/avelis/api/auth.py` and `backend/src/avelis/cache.py`; catch `RedisError` on auth paths to prevent site-wide 500 errors. Migrate auth transport to `HttpOnly + Secure + SameSite=Lax` cookies (`frontend/src/api/client.ts` and backend auth endpoints) while keeping Bearer compatibility.
- **Lightweight standard auth extensions**: Implement 1 generic standard OIDC flow (Authorization Code + PKCE + JIT provisioning reusing `authlib`, `pyjwt` and `httpx` already in `backend/pyproject.toml`), plus optional ~20-line reverse-proxy Forward Auth trusted headers, optional local TOTP two-factor auth and Personal Access Tokens (PAT).

### M1 acceptance focus (Planned)

- Backend security tests cover and pass all vulnerability fixes, anti-brute-force controls, `RedisError` tolerance, Cookie/Bearer auth, OIDC / Forward Auth / TOTP / PAT and admin user-management endpoints.
- An independent review agent scores Playwright desktop and mobile screenshots of the sign-in and password-change pages in both light and dark themes against an explicit visual and interaction rubric.

## M2: UI/UX, motion, accessibility and frontend performance (Planned)

`M2` closes broken UX loops, completes governance CRUD workflows, brings all semantic color tokens to WCAG AA, fixes keyboard and overlay accessibility, and reduces bundle size and layout thrashing.

### Navigation, retry and CRUD closures (Planned)

- **Cross-page parameters, retries and exports**: Fix parameter loss when `frontend/src/pages/catalog/MetricDetail.tsx` navigates to `/ask?metric_id=...` and load the preset context in `/analysis`; unify and converge legacy `/ask/:threadId` and `/analysis` routes; add one-click retry and edit-resend for `failed` and `stopped` states in `frontend/src/pages/analysis/AnalysisPage.tsx`, and add export, Copy SQL and deep-analysis entry points to `LookupAnswer` in the same file.
- **Global components and client resilience**: Add one-click Copy SQL to the global `Code` component in `frontend/src/components/ui.tsx`; unify Ask and follow-up submit shortcuts; wrap `logout()` in `try/finally` inside `frontend/src/api/client.ts`, consolidate 5 naked `fetch()` calls, forward React Query `AbortSignal`, add local `ErrorBoundary` wrappers and fix `RouteError`.
- **Navigation and workbench onboarding**: Add a `⌘K / Ctrl+K` global command palette; add a cold-start onboarding wizard in `frontend/src/pages/Workbench.tsx`; add route-level `document.title`, breadcrumbs and a multi-data-source error summary in the sidebar.
- **CRUD completeness, details and i18n**: Complete edit, delete, run-history, preserved model/skills and per-row loading for scheduled tasks, and edit, delete and test-connection for data sources; show all `definition_changed` diffs in `frontend/src/pages/changes/ChangesPage.tsx`; add filter reset and URL sync in `frontend/src/pages/changes/ChangeRecords.tsx`; distinguish `D3–D8` evidence dimension labels, prevent lineage edge label overlap (`frontend/src/pages/lineage/LineagePage.tsx`), add sidebar recent-chat fade and “view all”, and use human-readable skill card titles; fix `frontend/src/numbers.ts` emitting Simplified Chinese units in English/Traditional Chinese and `frontend/src/pages/ask/presentation.ts` matching Chinese-only headers.

### Design tokens, accessibility, motion and frontend performance (Planned)

- **Design system and WCAG AA contrast**: Fix light and dark semantic tokens in `frontend/design/tokens.json` (`--ok` on `--ok-subtle`, `--warn`, `--accent-text`, `--danger`, `--text-tertiary`, etc.) and expand `frontend/src/styles/contrast.test.mjs` to check every semantic token.
- **Keyboard and screen-reader accessibility (A11y)**: Introduce a unified overlay stack so `Esc` closes only the top-most layer when `Drawer` is stacked with `ConfirmDialog` or `Select`; focus Cancel by default on dangerous `ConfirmDialog` actions and add `aria-describedby`; fix the `DateInput` Tab trap, implement ARIA grid semantics and add year/month quick-pick or input; support arrow-key navigation and focus restoration in menus; add `role="alert"` / `role="status"` on `Banner`; add a Skip Link and global `:focus-visible`.
- **Motion cleanup and frontend performance**: Resolve the `@keyframes av-shimmer` name collision between `frontend/src/styles/app.css` and `frontend/src/styles/pages.css`; replace animations that repaint `width`, `flex-grow` or `background-position`; throttle `useReveal` in `frontend/src/pages/analysis/RunTranscript.tsx` to stop 60fps reflow; remove triple-stacked redundant motion on the workbench; fix `Tooltip` first-frame direction and `prefers-reduced-motion` gaps; add route-level `React.lazy` splitting and on-demand locale loading in `frontend/src/main.tsx` (cutting the ~1.21 MB main bundle by ≥ 50%); decouple `dagre.layout` from selection state in `LineagePage.tsx`; stop global polling of archived conversations in the sidebar; fix narrow-screen `.av-fill` clipping and switch `100vh` to `100dvh`.

### M2 acceptance focus (Planned)

- Expanded `contrast.test.mjs` covers all light and dark semantic tokens and passes WCAG AA checks.
- Frontend unit tests and Playwright E2E verify all navigation, retry, CRUD, i18n and keyboard A11y requirements; `vite build` main bundle size shrinks by ≥ 50%.

## M3: Agent intelligence, evaluation and MCP / Skills (Planned)

`M3` upgrades Ask and analysis retrieval accuracy, multi-provider streaming, cost governance, evaluation scores, read-only execution and MCP / Skill safety.

### Hybrid retrieval and organizational knowledge flywheel (Planned)

- **Hybrid retrieval and Verified Query Repository (VQR)**: Combine semantic vector search with lexical fallback in `backend/src/avelis/ask/service.py`, `backend/src/avelis/analysis/tools.py` and `backend/src/avelis/analysis/retrieval.py`; add a Golden SQL / Verified Query Repository (VQR).
- **Domain-reviewed experiences and Ask feedback**: Upgrade personal experiences in `backend/src/avelis/governance/experiences.py` to support team and business-domain review and sharing (blocking self-approval); add thumbs up/down feedback, long-thread compaction, numeric grounding verification for explanations and optional semantic caching to Ask.

### Multi-provider streaming and cost ledger (Planned)

- **Provider-agnostic streaming and failover**: Decouple `backend/src/avelis/modelcfg.py` and `backend/src/avelis/llm_protocol.py` from DeepSeek-only bindings to unify OpenAI, Anthropic and compatible streaming with thinking/reasoning effort controls; add primary/backup failover and complexity-based routing.
- **SSE streaming and cost quotas**: Implement backend and frontend SSE real-time streaming for both Ask and Analysis; complete the token and cost ledger, pricing configuration and per-user / per-domain quotas and rate limits.

### Analysis depth, evaluation target and MCP / Skills hardening (Planned)

- **Evaluation uplift to ≥ 70%**: Use `scripts/eval_analysis.py` to raise overall pass rate to ≥ 70%—specifically addressing exploratory (`12.5%` baseline) and negative (`25.0%` baseline) questions—and add an Ask evaluation suite with numeric grounding tests.
- **Read-only sandbox and multi-engine exploration**: Extend `query_sql` in `backend/src/avelis/analysis/explore.py` across all supported query engines (beyond the partial baseline) with window-function support; add a controlled read-only Python / DuckDB sandbox for computation and enable parallel execution of read-only tools in `backend/src/avelis/analysis/agent.py`.
- **MCP negotiation and Skill security**: Replace the hard-locked single `Mcp-Protocol-Version` check in `backend/src/avelis/mcp/auth.py` with backward-compatible version negotiation; enhance `ask_metric` in `backend/src/avelis/mcp/tools.py` with multi-metric, `order` and `align_filters` support; in `backend/src/avelis/skills/`, block admins from approving their own uploaded skills, add upload rate limiting and run prompt-injection / sensitive-word pre-checks.

## M4: Data security compliance, observability and CI (Planned)

`M4` adds enterprise transport security, PII masking, compliance auditing, structured observability and container-backed database CI.

### Connector hardening, key rotation and data masking (Planned)

- **TLS and read-only sessions across all 8 connectors**: Add TLS/SSL and CA certificate configuration across all 8 connectors in `backend/src/avelis/connectors/` (StarRocks, Doris, MySQL, PostgreSQL, ClickHouse, SQL Server, Oracle, Hive), and add session-level read-only enforcement for StarRocks, Doris and ClickHouse.
- **Dual-key rotation, PII masking and RLS**: Implement a genuine `MultiFernet` dual-key rotation CLI in `backend/src/avelis/security.py` and `backend/src/avelis/bootstrap.py`; add column-level PII tagging and masking (in UI display and before outbound model calls); align the `send_results` safe default policy; add basic Row-Level Security (RLS) expression injection.

### Audit extensions, hash-chain verification and retention (Planned)

- **Audit context and export tracking**: Populate `client_ip`, `user_agent` and `request_id` on `AuditEvent`; log answer views and CSV / PDF / HTML exports; expand security webhook events; build an admin audit-log query and hash-chain verification page in the UI.
- **Retention purge and user anonymization**: Implement `Answer` and `AnalysisDataset` retention cleanup and departed-user anonymization in `backend/src/avelis/history/retention.py`.

### Observability, CI pipeline and dependency convergence (Planned)

- **Logs, metrics and tracing**: Add structured JSON logging, a Prometheus `/metrics` endpoint and optional OpenTelemetry integration.
- **Full integration CI and `sqlglot` convergence**: Extend `.github/workflows/ci.yml` with PostgreSQL and Valkey service containers to run `backend/tests/db` and `backend/tests/metadb`, Docker smoke tests, security vulnerability scans and Playwright E2E; converge the personal `sqlglot` fork in `backend/pyproject.toml`.

## M5: 10k-table scale and multi-replica HA (Planned, final phase)

> **Gate requirement**: `M5` must be implemented strictly last, only after `M1`, `M2`, `M3` and `M4` are all completed and verified.

The `v1.0.11` baseline uses a single global `history_chain_head` row lock, `LOCK TABLE` purges, full-graph fingerprint recomputation and local filesystem skill storage. `M5` refactors these bottlenecks for 10k-table metadata scale and multi-replica high availability:

### Hash chain and history retention scalability (Planned)

- **Lock-free hash chain**: Replace the per-row global `history_chain_head FOR UPDATE` trigger lock in `backend/src/avelis/history/integrity.py` with batch/transaction-level Merkle anchoring or sharded hash chains.
- **Streaming cursor verify and purge**: Refactor `backend/src/avelis/history/retention.py` to use cursor-based streaming verification and retention purges without `LOCK TABLE`.

### 10k-table catalog collection and search indexes (Planned)

- **Incremental collection and local subgraph recomputation**: Implement incremental metadata collection, batch upserts and local affected-subgraph lineage and checksum (fingerprint) recomputation.
- **Full-text and fuzzy search indexes**: Add PostgreSQL `pg_trgm` catalog search indexes and `Answer` GIN indexes; optimize Apache Hive batch metadata fetching and target database connection reuse.

### Multi-replica HA and cloud-native deployment (Planned)

- **Multi-slot isolated workers and cross-replica SSE**: Add multi-slot concurrency, queue-isolated workers and instance-ID heartbeats in `backend/src/avelis/jobs.py` and `backend/src/avelis/cache.py`; eliminate synchronous blocking `?wait=true` queries and support cross-replica SSE via Redis Pub/Sub.
- **Object storage and Helm zero-downtime ops**: Add an S3 / MinIO object storage backend for skills and exports; provide a Kubernetes Helm chart (`deploy/helm/`), a pre-deploy migration job, `CREATE INDEX CONCURRENTLY` migrations and graceful shutdown disposal of database engines and Redis pools.

## Ongoing directions (Planned)

Alongside the five sequential phases above, the following published directions remain active without target dates:

- **External catalogs and materialized views for more engines**: Continue expanding metadata collection and lineage parsing across engines.
- **Richer Ask expressions**: Nested derived metrics, week and month grains, and share of total.

## Verification and references

| Resource or reference | Purpose |
|---|---|
| [`frontend/design/tokens.json`](../frontend/design/tokens.json) and [`contrast.test.mjs`](../frontend/src/styles/contrast.test.mjs) | Light and dark semantic token WCAG AA contrast verification |
| [`scripts/eval_analysis.py`](../scripts/eval_analysis.py) | Analysis agent evaluation script (`M3` ≥ 70% pass-rate gate) |
| [`backend/tests/db`](../backend/tests/db) and [`backend/tests/metadb`](../backend/tests/metadb) | PostgreSQL and metadata database test suites (`M4` container CI target) |
| [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) | Repository lint, unit, build and planned integration / security / E2E pipeline |
| [Architecture and governance](architecture.en.md) and [ADR 0001–0022](decisions/) (Simplified Chinese) | Current `v1.0.11` baseline architecture and historical decision context |
