# 可观测性与生产排障指南（TraceID / 日志规范 / Prometheus Metrics）

本文档说明 Avelis 在生产部署与线上排障时的链路追踪（`trace_id` / `request_id`）、结构化日志规范与 Prometheus `/metrics` 监控指标口径。

> **模块化代码布局**：
> - `backend/src/avelis/observability/context.py`：`RequestContextMiddleware`、W3C `traceparent` / `X-Trace-ID` / `X-Request-ID` 解析、上下文 `ContextVar` 绑定与传播
> - `backend/src/avelis/observability/logging.py`：`SecretRedactingFilter`、`JsonLogFormatter`、`TextLogFormatter`、`configure_logging`、`build_uvicorn_log_config`
> - `backend/src/avelis/observability/metrics.py`：`MetricsRegistry` 与 `METRICS`（HTTP / Ask / LLM / Datasource / Job / 连接池与构建信息指标）
> - `backend/src/avelis/observability/middleware.py`：`MetricsMiddleware`（指标采集与分级 `avelis.access` 访问日志）
> - `backend/src/avelis/observability/tracing.py`：可选 OpenTelemetry `init_tracing` 钩子
> - `backend/src/avelis/api/metrics.py`：`GET /metrics` 端点注册与会话复用

---

## 1. 全链路追踪（TraceID & RequestID）

### 1.1 请求头与传播约定

每个 HTTP/WebSocket 请求经过最外层 `RequestContextMiddleware` 时，会解析并生成全链路标识，并在响应头原样回传：

| 标识 | 传入头（优先级从高到低） | 合法字符规则 | 缺失或非法时回退 | 响应头回传 |
| --- | --- | --- | --- | --- |
| `trace_id` | 1. `traceparent`（W3C `00-<32hex>-<16hex>-<2hex>`，拒绝全零）<br>2. `X-Trace-ID` | `^[A-Za-z0-9._-]{1,64}$` | 自动生成 32 位小写 UUID hex | `X-Trace-ID` + `traceparent`（当 `trace_id` 为 32 位 hex 时） |
| `span_id` | 每次入站/任务单独生成 | 16 位 hex | `uuid4().hex[:16]` | 写入 `traceparent` 响应头与日志 |
| `request_id` | `X-Request-ID` | `^[A-Za-z0-9._-]{1,64}$` | 自动生成 32 位小写 UUID hex | `X-Request-ID` |
| `user_id` | 认证依赖自动绑定（Cookie / JWT / PAT） | 内部用户 ID | 未认证请求不输出 | 写入日志上下文 |
| `job_id` | 后台任务领取时自动绑定 | `job.id` | 仅限 Worker 任务期间存在 | 写入日志上下文与审计详情 |

### 1.2 跨域、反向代理与后台 Worker 串联

1. **Nginx / 网关配置示例**：确保透传客户端真实地址与上游链路头，并将 `AVELIS_FORWARDED_ALLOW_IPS` 配为网关网段（如 `10.0.0.0/8,127.0.0.1`）：
   ```nginx
   proxy_set_header Host $host;
   proxy_set_header X-Real-IP $remote_addr;
   proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
   proxy_set_header X-Forwarded-Proto $scheme;
   proxy_set_header X-Request-ID $request_id;
   ```
2. **API → Worker 异步传递**：通过 `avelis.jobs._enqueue(...)` 入队的任务会自动携带当前上下文的 `trace_id` 与 `request_id`（存放在内部 `_trace` 元数据中）。Worker 执行 `run_job()` 时自动恢复同一 `trace_id` 并生成新 `span_id`，所有采集/导入/分析日志自动带上同一 `trace_id`。
3. **日志 ↔ 防篡改审计联动**：每次 `audit.record()` 会自动将当前 `trace_id`、`request_id`、`client_ip`、`user_agent`（及后台任务的 `job_id`）封入 `audit_event.detail`，参与全库只追加哈希链（`row_hash`）摘要计算。排障时可直接用 `GET /api/audit?trace_id=<trace_id>` 或在“设置 → 审计与验真”页按 `request_id` 检索全部相关动作。

---

## 2. 生产日志规范（Logging）

### 2.1 配置项

- `AVELIS_LOG_LEVEL`（默认 `info`）：`critical` / `error` / `warning` / `info` / `debug` / `trace`
- `AVELIS_LOG_FORMAT`（默认 `text`）：
  - `text`：本地与研发调试使用，格式为 `%(asctime)s %(levelname)s [%(name)s] [trace=<12> req=<12> uid=<8> job=<8>] <message>`
  - `json`：**生产推荐**，单行 UTF-8 JSON，包含稳定结构化字段，Uvicorn 主服务与 Worker 均遵循此配置（且禁用 Uvicorn 原生冗余 `uvicorn.access`，统一由 `avelis.access` 输出单条带 Trace 上下文与 `duration_ms` 的访问日志）。

### 2.2 JSON 日志标准 Schema

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

### 2.3 日志级别与自动脱敏策略

- **HTTP 访问日志分级（`avelis.access`）**：
  - 未捕获异常或 `status_code >= 500`：`ERROR`（附 `error_type`、`error_message`、`exception`）
  - `4xx` 响应：`WARNING`
  - 健康检查与指标端点（`/api/health`、`/api/health/ready`、`/api/health/worker`、`/metrics`）成功响应：`DEBUG`（避免 200 探针淹没生产 `INFO` 日志）
  - 其余正常请求：`INFO`
- **内置脱敏过滤器（`SecretRedactingFilter`）**：所有日志消息、`extra` 字段与异常堆栈都会自动掩码：
  - `Bearer <token>` / `Basic <token>` → `Bearer [REDACTED]` / `Basic [REDACTED]`
  - `sk-...`、`pat_...`、`avp_...` 凭据前缀 → `[REDACTED]`
  - URL / DSN 内嵌口令 `scheme://user:password@host` → `scheme://user:***@host`
  - `password=...`、`secret=...`、`api_key=...` 等敏感键值对 → `<key>=[REDACTED]`

### 2.4 Loki / ELK 常见检索

```logql
# 按 trace_id 串联一次问数请求与后台采集全过程
{app="avelis"} | json | trace_id="4bf92f3577b34da6a3ce929d0e0e4736"

# 排查耗时 > 2s 的慢请求或 5xx 异常
{app="avelis"} | json | logger="avelis.access" | (status_code >= 500 or duration_ms > 2000)
```

---

## 3. Prometheus 运行指标（`/metrics`）

### 3.1 开启与访问控制

`GET /metrics` 默认关闭（`AVELIS_METRICS_ENABLED=false`，返回 `404`；不会被前端 SPA 吞掉）。开启后提供 Prometheus Text Exposition v0.0.4 格式，且受双重鉴权保护：

- `AVELIS_METRICS_ENABLED=true`
- **推荐生产抓取方式**：设置 `AVELIS_METRICS_TOKEN`（或 `AVELIS_METRICS_TOKEN_FILE`），Prometheus 使用 `Authorization: Bearer <token>` 抓取，该路径完全绕过数据库查询，零额外连接池消耗。
- 未配置 `AVELIS_METRICS_TOKEN`（或令牌不匹配）时，回退校验当前请求是否为已登录的 `admin` 管理员（未登录返回 `401`，非管理员返回 `403`）。

```yaml
# prometheus.yml 片段
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

### 3.2 指标字典

| 指标名 | 类型 | 标签 | 说明 |
| --- | --- | --- | --- |
| `avelis_http_requests_total` | Counter | `method`, `path`, `status` | HTTP 请求总数；`path` 使用归一化路由模板，未匹配路径统一收敛为 `unmatched` 防高基数 |
| `avelis_http_request_duration_seconds` | Histogram | `method`, `path` | HTTP 响应延迟直方图 |
| `avelis_http_requests_in_flight` | Gauge | — | 当前正在处理的 HTTP 并发请求数 |
| `avelis_ask_requests_total` | Counter | `status` | 问数执行总次数（`answered` / `blocked` / `clarify` / `denied` / `error`） |
| `avelis_ask_duration_seconds` | Histogram | `status` | 问数端到端耗时分布 |
| `avelis_llm_calls_total` | Counter | `purpose`, `status` | 模型调用次数 |
| `avelis_llm_call_duration_seconds` | Histogram | `purpose` | 模型调用延迟分布 |
| `avelis_llm_tokens_total` | Counter | `purpose`, `type` | 累计消耗 Token（`type`：`input` / `output` / `cache_hit`） |
| `avelis_datasource_queries_total` | Counter | `engine`, `status` | 只读数据源查询次数（问数 + 分析探索） |
| `avelis_datasource_query_duration_seconds` | Histogram | `engine` | 只读数据源查询延迟分布 |
| `avelis_job_runs_total` | Counter | `kind`, `status` | 后台任务（采集 / 导入 / 分析）终态计数 |
| `avelis_job_duration_seconds` | Histogram | `kind` | 后台任务执行耗时分布 |
| `avelis_db_pool_connections` | Gauge | `state` | 元数据库 PG 连接池快照（`size` / `checked_in` / `checked_out` / `overflow`） |
| `avelis_process_uptime_seconds` | Gauge | — | 进程运行时长（秒） |
| `avelis_build_info` | Gauge | `version`, `environment` | 构建版本与运行环境元信息（恒为 1） |

### 3.3 推荐 PromQL 告警规则

```promql
# 1. API 5xx 错误率 > 2%（持续 5 分钟）
sum(rate(avelis_http_requests_total{status=~"5.."}[5m])) / clamp_min(sum(rate(avelis_http_requests_total[5m])), 1e-6) > 0.02

# 2. HTTP P95 延迟 > 3s（排除长时间问数/分析流以外或单独监控 /api/ask）
histogram_quantile(0.95, sum by (le) (rate(avelis_ask_duration_seconds_bucket[5m]))) > 10

# 3. LLM 调用失败率 > 10%
sum(rate(avelis_llm_calls_total{status!="ok"}[10m])) / clamp_min(sum(rate(avelis_llm_calls_total[10m])), 1e-6) > 0.1

# 4. 元数据库连接池借出耗尽（checkout >= size）
avelis_db_pool_connections{state="checked_out"} >= on() group_left() avelis_db_pool_connections{state="size"}
```

---

## 4. 可选 OpenTelemetry 导出

如需接入 Jaeger / Tempo / Datadog APM，可在安装 `opentelemetry-sdk`、`opentelemetry-instrumentation-fastapi`、`opentelemetry-exporter-otlp` 后开启：

```env
AVELIS_OTEL_ENABLED=true
AVELIS_OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318/v1/traces
```

未安装上述可选依赖时，应用仅输出一条告警日志并降级为内置 TraceID + Prometheus 指标模式，不影响主流程启动。
