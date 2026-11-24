# Observability and Production Troubleshooting Guide (TraceID / Logging / Prometheus Metrics)

English | [简体中文](observability.md) | [繁體中文](observability.zh-TW.md)

This document explains request tracing (`trace_id` / `request_id`), structured logging conventions, and Prometheus `/metrics` definitions for operating and troubleshooting Avelis in production.

> **Modular code layout**:
> - `backend/src/avelis/observability/context.py`: `RequestContextMiddleware`, W3C `traceparent` / `X-Trace-ID` / `X-Request-ID` parsing, and `ContextVar` binding & propagation
> - `backend/src/avelis/observability/logging.py`: `SecretRedactingFilter`, `JsonLogFormatter`, `TextLogFormatter`, `configure_logging`, `build_uvicorn_log_config`
> - `backend/src/avelis/observability/metrics.py`: `MetricsRegistry` and `METRICS` (HTTP / Ask / LLM / Datasource / Job / DB pool & build-info metrics)
> - `backend/src/avelis/observability/middleware.py`: `MetricsMiddleware` (metric recording and tiered `avelis.access` access logging)
> - `backend/src/avelis/observability/tracing.py`: optional OpenTelemetry `init_tracing` hook
> - `backend/src/avelis/api/metrics.py`: `GET /metrics` route registration and session reuse

---

## 1. End-to-End Request Tracing (`trace_id` & `request_id`)

### 1.1 Headers and Propagation Contract

Every HTTP/WebSocket request passes through `RequestContextMiddleware`, which extracts or generates correlation IDs and echoes them back on the HTTP response headers:

| Identifier | Inbound Headers (Highest Priority First) | Validation Rule | Fallback When Missing or Invalid | Response Headers |
| --- | --- | --- | --- | --- |
| `trace_id` | 1. `traceparent` (W3C `00-<32hex>-<16hex>-<2hex>`, all-zero IDs rejected)<br>2. `X-Trace-ID` | `^[A-Za-z0-9._-]{1,64}$` | Generated 32-char lowercase UUID hex | `X-Trace-ID` + `traceparent` (when `trace_id` is 32 hex chars) |
| `span_id` | Generated per inbound request / job | 16 hex chars | `uuid4().hex[:16]` | Written into `traceparent` response header and logs |
| `request_id` | `X-Request-ID` | `^[A-Za-z0-9._-]{1,64}$` | Generated 32-char lowercase UUID hex | `X-Request-ID` |
| `user_id` | Bound automatically by auth dependencies (Cookie / JWT / PAT) | Internal user ID | Omitted for unauthenticated requests | Written to log context |
| `job_id` | Bound automatically when a worker claims a job | `job.id` | Present only during worker execution | Written to log context and audit details |

### 1.2 Reverse Proxies, Workers and Tamper-Evident Audit Correlation

1. **Nginx / gateway example**: Forward real client IP and upstream tracing headers, and configure `AVELIS_FORWARDED_ALLOW_IPS` with trusted proxy CIDRs/addresses (for example `10.0.0.0/8,127.0.0.1`):
   ```nginx
   proxy_set_header Host $host;
   proxy_set_header X-Real-IP $remote_addr;
   proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
   proxy_set_header X-Forwarded-Proto $scheme;
   proxy_set_header X-Request-ID $request_id;
   ```
2. **API → Worker asynchronous propagation**: Jobs enqueued via `avelis.jobs._enqueue(...)` attach the active `trace_id` and `request_id` inside internal `_trace` metadata. When the worker executes `run_job()`, it restores the same `trace_id` and starts a new `span_id`, keeping collection, import, and analysis logs linked under the same trace.
3. **Logs ↔ Audit hash chain**: `audit.record()` embeds the active `trace_id`, `request_id`, `client_ip`, `user_agent` (and `job_id` inside worker tasks) into `audit_event.detail`, which is hashed into the append-only `row_hash` chain. For incident response, query `GET /api/audit?trace_id=<trace_id>` or search by `request_id` under **Settings → Audit & Verification**.

---

## 2. Production Logging Conventions

### 2.1 Configuration Options

- `AVELIS_LOG_LEVEL` (default `info`): `critical` / `error` / `warning` / `info` / `debug` / `trace`
- `AVELIS_LOG_FORMAT` (default `text`):
  - `text`: Readable local/development format `%(asctime)s %(levelname)s [%(name)s] [trace=<12> req=<12> uid=<8> job=<8>] <message>`
  - `json`: **Recommended for production**. Emits single-line UTF-8 JSON across both the API server and worker processes (native `uvicorn.access` is disabled in favor of unified `avelis.access` records carrying trace context and `duration_ms`).

### 2.2 Standard JSON Log Schema

```json
{
  "timestamp": "2026-11-24T07:15:01.234Z",
  "level": "INFO",
  "service": "avelis-api",
  "env": "production",
  "logger": "avelis.access",
  "message": "POST /api/ask -> 200 (184.52ms)",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "span_id": "a1b2c3d4e5f60718",
  "parent_span_id": "00f067aa0ba902b7",
  "request_id": "req-Gateway-001",
  "user_id": "u_alice",
  "client_ip": "203.0.113.9",
  "event": "http.access",
  "method": "POST",
  "path": "/api/ask",
  "route": "/api/ask",
  "status_code": 200,
  "duration_ms": 184.52
}
```

### 2.3 Tiered Log Levels and Automatic Redaction

- **HTTP access log levels (`avelis.access`)**:
  - Unhandled exceptions or `status_code >= 500`: `ERROR` (with `error_type`, `error_message`, `exception`)
  - `4xx` responses: `WARNING`
  - Successful probes and scrape requests (`/api/health`, `/api/health/ready`, `/api/health/worker`, `/metrics`): `DEBUG` (avoids flooding `INFO` logs)
  - All other normal responses: `INFO`
- **Built-in redaction (`SecretRedactingFilter`)**: Masks secrets in messages, `extra` fields, and stack traces:
  - `Bearer <token>` / `Basic <token>` → `Bearer [REDACTED]` / `Basic [REDACTED]`
  - Credential prefixes (`sk-...`, `pat_...`, `avp_...`) → `[REDACTED]`
  - Embedded URL / DSN passwords (`scheme://user:password@host`) → `scheme://user:***@host`
  - Sensitive key/value pairs (`password=...`, `secret=...`, `api_key=...`) → `<key>=[REDACTED]`

### 2.4 Common Loki / ELK Queries

```logql
# Follow an Ask request and any triggered background jobs by trace_id
{app="avelis"} | json | trace_id="4bf92f3577b34da6a3ce929d0e0e4736"

# Find slow (> 2s) or failing (5xx) HTTP requests
{app="avelis"} | json | logger="avelis.access" | (status_code >= 500 or duration_ms > 2000)
```

---

## 3. Prometheus Metrics (`/metrics`)

### 3.1 Enabling and Access Control

`GET /metrics` is disabled by default (`AVELIS_METRICS_ENABLED=false`, returning `404`; never swallowed by the SPA fallback). When enabled, it exports Prometheus Text Exposition v0.0.4 behind dual-path auth:

- Set `AVELIS_METRICS_ENABLED=true`.
- **Recommended for production scrapers**: Configure `AVELIS_METRICS_TOKEN` (or `AVELIS_METRICS_TOKEN_FILE`) and pass `Authorization: Bearer <token>`. Scrapes with this token bypass database session checks completely.
- When `AVELIS_METRICS_TOKEN` is unset (or does not match), the endpoint requires an authenticated `admin` session (returning `401` if unauthenticated or `403` for non-admin users).

```yaml
# prometheus.yml snippet
scrape_configs:
  - job_name: avelis
    metrics_path: /metrics
    scheme: https
    authorization:
      type: Bearer
      credentials_file: /run/secrets/avelis_metrics_token
    static_configs:
      - targets: ["avelis.internal:8000"]
```

### 3.2 Metrics Dictionary

| Metric | Type | Labels | Description |
| --- | --- | --- | --- |
| `avelis_http_requests_total` | Counter | `method`, `path`, `status` | Total HTTP requests; `path` uses normalized route templates, with unmatched paths grouped under `unmatched` to avoid high cardinality |
| `avelis_http_request_duration_seconds` | Histogram | `method`, `path` | HTTP request latency distribution |
| `avelis_http_requests_in_flight` | Gauge | — | In-flight HTTP requests |
| `avelis_ask_requests_total` | Counter | `status` | Total Ask executions (`answered` / `blocked` / `clarify` / `denied` / `error`) |
| `avelis_ask_duration_seconds` | Histogram | `status` | Ask end-to-end duration distribution |
| `avelis_llm_calls_total` | Counter | `purpose`, `status` | Total outbound LLM calls |
| `avelis_llm_call_duration_seconds` | Histogram | `purpose` | Outbound LLM call duration distribution |
| `avelis_llm_tokens_total` | Counter | `purpose`, `type` | Cumulative LLM tokens (`type`: `input` / `output` / `cache_hit`) |
| `avelis_datasource_queries_total` | Counter | `engine`, `status` | Read-only data-source queries (Ask + Analysis exploration) |
| `avelis_datasource_query_duration_seconds` | Histogram | `engine` | Read-only data-source query duration distribution |
| `avelis_job_runs_total` | Counter | `kind`, `status` | Background job completions (`collect` / `import` / `analysis`) |
| `avelis_job_duration_seconds` | Histogram | `kind` | Background job duration distribution |
| `avelis_db_pool_connections` | Gauge | `state` | Metadata PostgreSQL pool snapshot (`size` / `checked_in` / `checked_out` / `overflow`) |
| `avelis_process_uptime_seconds` | Gauge | — | Process uptime in seconds |
| `avelis_build_info` | Gauge | `version`, `environment` | Build version and runtime environment info (constant `1`) |

### 3.3 Recommended PromQL Alert Rules

```promql
# 1. API 5xx error ratio > 2% over 5m
sum(rate(avelis_http_requests_total{status=~"5.."}[5m])) / clamp_min(sum(rate(avelis_http_requests_total[5m])), 1e-6) > 0.02

# 2. Ask P95 latency > 10s over 5m
histogram_quantile(0.95, sum by (le) (rate(avelis_ask_duration_seconds_bucket[5m]))) > 10

# 3. LLM call failure ratio > 10% over 10m
sum(rate(avelis_llm_calls_total{status!="ok"}[10m])) / clamp_min(sum(rate(avelis_llm_calls_total[10m])), 1e-6) > 0.1

# 4. Metadata DB pool saturation (checked_out >= size)
avelis_db_pool_connections{state="checked_out"} >= on() group_left() avelis_db_pool_connections{state="size"}
```

---

## 4. Optional OpenTelemetry Export

To export spans to Jaeger, Tempo, or Datadog APM, install `opentelemetry-sdk`, `opentelemetry-instrumentation-fastapi`, and `opentelemetry-exporter-otlp`, then enable:

```env
AVELIS_OTEL_ENABLED=true
AVELIS_OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318/v1/traces
```

If those optional packages are not installed, Avelis logs a single warning and continues operating normally with built-in TraceID propagation and Prometheus metrics.