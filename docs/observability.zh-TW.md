# 可觀測性與正式環境排障指南（TraceID / 日誌規範 / Prometheus Metrics）

[English](observability.en.md) | [简体中文](observability.md) | 繁體中文

本文件說明 Avelis 在正式部署與線上排障時的鏈路追蹤（`trace_id` / `request_id`）、結構化日誌規範與 Prometheus `/metrics` 監控指標口徑。

> **模組化程式碼佈局**：
> - `backend/src/avelis/observability/context.py`：`RequestContextMiddleware`、W3C `traceparent` / `X-Trace-ID` / `X-Request-ID` 解析、上下文 `ContextVar` 繫結與傳播
> - `backend/src/avelis/observability/logging.py`：`SecretRedactingFilter`、`JsonLogFormatter`、`TextLogFormatter`、`configure_logging`、`build_uvicorn_log_config`
> - `backend/src/avelis/observability/metrics.py`：`MetricsRegistry` 與 `METRICS`（HTTP / Ask / LLM / Datasource / Job / 連線池與建置資訊指標）
> - `backend/src/avelis/observability/middleware.py`：`MetricsMiddleware`（指標採集與分級 `avelis.access` 存取日誌）
> - `backend/src/avelis/observability/tracing.py`：可選 OpenTelemetry `init_tracing` 掛鉤
> - `backend/src/avelis/api/metrics.py`：`GET /metrics` 端點註冊與工作階段複用

---

## 1. 全鏈路追蹤（TraceID & RequestID）

### 1.1 請求標頭與傳播約定

每個 HTTP/WebSocket 請求經過最外層 `RequestContextMiddleware` 時，會解析並產生全鏈路識別碼，並在回應標頭原樣回傳：

| 識別碼 | 傳入標頭（優先序由高到低） | 合法字元規則 | 缺失或非法時回退 | 回應標頭回傳 |
| --- | --- | --- | --- | --- |
| `trace_id` | 1. `traceparent`（W3C `00-<32hex>-<16hex>-<2hex>`，拒絕全零）<br>2. `X-Trace-ID` | `^[A-Za-z0-9._-]{1,64}$` | 自動產生 32 位小寫 UUID hex | `X-Trace-ID` + `traceparent`（當 `trace_id` 為 32 位 hex 時） |
| `span_id` | 每次入站/任務單獨產生 | 16 位 hex | `uuid4().hex[:16]` | 寫入 `traceparent` 回應標頭與日誌 |
| `request_id` | `X-Request-ID` | `^[A-Za-z0-9._-]{1,64}$` | 自動產生 32 位小寫 UUID hex | `X-Request-ID` |
| `user_id` | 認證依賴自動繫結（Cookie / JWT / PAT） | 內部使用者 ID | 未認證請求不輸出 | 寫入日誌上下文 |
| `job_id` | 後臺任務領取時自動繫結 | `job.id` | 僅限 Worker 任務期間存在 | 寫入日誌上下文與稽核詳情 |

### 1.2 跨域、反向代理與後臺 Worker 串聯

1. **Nginx / 閘道組態範例**：確保透傳用戶端真實位址與上游鏈路標頭，並將 `AVELIS_FORWARDED_ALLOW_IPS` 設為閘道網段（如 `10.0.0.0/8,127.0.0.1`）：
   ```nginx
   proxy_set_header Host $host;
   proxy_set_header X-Real-IP $remote_addr;
   proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
   proxy_set_header X-Forwarded-Proto $scheme;
   proxy_set_header X-Request-ID $request_id;
   ```
2. **API → Worker 非同步傳遞**：透過 `avelis.jobs._enqueue(...)` 入列的任務會自動攜帶目前上下文的 `trace_id` 與 `request_id`（存放在內部 `_trace` 中繼資料中）。Worker 執行 `run_job()` 時自動恢復同一 `trace_id` 並產生新 `span_id`，所有採集/匯入/分析日誌自動帶上同一 `trace_id`。
3. **日誌 ↔ 防竄改稽核聯動**：每次 `audit.record()` 會自動將目前 `trace_id`、`request_id`、`client_ip`、`user_agent`（及後臺任務的 `job_id`）封入 `audit_event.detail`，參與全庫只追加雜湊鏈（`row_hash`）摘要計算。排障時可直接用 `GET /api/audit?trace_id=<trace_id>` 或在「設定 → 稽核與驗真」頁依 `request_id` 檢索全部相關動作。

---

## 2. 正式環境日誌規範（Logging）

### 2.1 設定項

- `AVELIS_LOG_LEVEL`（預設 `info`）：`critical` / `error` / `warning` / `info` / `debug` / `trace`
- `AVELIS_LOG_FORMAT`（預設 `text`）：
  - `text`：本機與研發除錯使用，格式為 `%(asctime)s %(levelname)s [%(name)s] [trace=<12> req=<12> uid=<8> job=<8>] <message>`
  - `json`：**正式環境推薦**，單行 UTF-8 JSON，包含穩定結構化欄位，Uvicorn 主服務與 Worker 均遵循此設定（且停用 Uvicorn 原生冗餘 `uvicorn.access`，統一由 `avelis.access` 輸出單條帶 Trace 上下文與 `duration_ms` 的存取日誌）。

### 2.2 JSON 日誌標準 Schema

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

### 2.3 日誌級別與自動遮罩原則

- **HTTP 存取日誌分級（`avelis.access`）**：
  - 未捕捉例外或 `status_code >= 500`：`ERROR`（附 `error_type`、`error_message`、`exception`）
  - `4xx` 回應：`WARNING`
  - 健康檢查與指標端點（`/api/health`、`/api/health/ready`、`/api/health/worker`、`/metrics`）成功回應：`DEBUG`（避免 200 探針淹沒正式 `INFO` 日誌）
  - 其餘正常請求：`INFO`
- **內建遮罩過濾器（`SecretRedactingFilter`）**：所有日誌訊息、`extra` 欄位與例外堆疊都會自動遮罩：
  - `Bearer <token>` / `Basic <token>` → `Bearer [REDACTED]` / `Basic [REDACTED]`
  - `sk-...`、`pat_...`、`avp_...` 憑證前綴 → `[REDACTED]`
  - URL / DSN 內嵌密碼 `scheme://user:password@host` → `scheme://user:***@host`
  - `password=...`、`secret=...`、`api_key=...` 等敏感鍵值對 → `<key>=[REDACTED]`

### 2.4 Loki / ELK 常見檢索

```logql
# 依 trace_id 串聯一次問數請求與後臺採集全過程
{app="avelis"} | json | trace_id="4bf92f3577b34da6a3ce929d0e0e4736"

# 排查耗時 > 2s 的慢請求或 5xx 例外
{app="avelis"} | json | logger="avelis.access" | (status_code >= 500 or duration_ms > 2000)
```

---

## 3. Prometheus 執行指標（`/metrics`）

### 3.1 開啟與存取控制

`GET /metrics` 預設關閉（`AVELIS_METRICS_ENABLED=false`，回傳 `404`；不會被前端 SPA 吞掉）。開啟後提供 Prometheus Text Exposition v0.0.4 格式，且受雙重鑑權保護：

- `AVELIS_METRICS_ENABLED=true`
- **推薦正式環境抓取方式**：設定 `AVELIS_METRICS_TOKEN`（或 `AVELIS_METRICS_TOKEN_FILE`），Prometheus 使用 `Authorization: Bearer <token>` 抓取，該路徑完全繞過資料庫查詢，零額外連線池消耗。
- 未設定 `AVELIS_METRICS_TOKEN`（或權杖不相符）時，回退驗證目前請求是否為已登入的 `admin` 管理員（未登入回傳 `401`，非管理員回傳 `403`）。

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

### 3.2 指標字典

| 指標名 | 類型 | 標籤 | 說明 |
| --- | --- | --- | --- |
| `avelis_http_requests_total` | Counter | `method`, `path`, `status` | HTTP 請求總數；`path` 使用歸一化路由範本，未匹配路徑統一收斂為 `unmatched` 防高基數 |
| `avelis_http_request_duration_seconds` | Histogram | `method`, `path` | HTTP 回應延遲直方圖 |
| `avelis_http_requests_in_flight` | Gauge | — | 目前正在處理的 HTTP 並行請求數 |
| `avelis_ask_requests_total` | Counter | `status` | 問數執行總次數（`answered` / `blocked` / `clarify` / `denied` / `error`） |
| `avelis_ask_duration_seconds` | Histogram | `status` | 問數端到端耗時分佈 |
| `avelis_llm_calls_total` | Counter | `purpose`, `status` | 模型呼叫次數 |
| `avelis_llm_call_duration_seconds` | Histogram | `purpose` | 模型呼叫延遲分佈 |
| `avelis_llm_tokens_total` | Counter | `purpose`, `type` | 累計消耗 Token（`type`：`input` / `output` / `cache_hit`） |
| `avelis_datasource_queries_total` | Counter | `engine`, `status` | 唯讀資料來源查詢次數（問數 + 分析探索） |
| `avelis_datasource_query_duration_seconds` | Histogram | `engine` | 唯讀資料來源查詢延遲分佈 |
| `avelis_job_runs_total` | Counter | `kind`, `status` | 後臺任務（採集 / 匯入 / 分析）終態計數 |
| `avelis_job_duration_seconds` | Histogram | `kind` | 後臺任務執行耗時分佈 |
| `avelis_db_pool_connections` | Gauge | `state` | 中繼資料庫 PG 連線池快照（`size` / `checked_in` / `checked_out` / `overflow`） |
| `avelis_process_uptime_seconds` | Gauge | — | 程序執行時長（秒） |
| `avelis_build_info` | Gauge | `version`, `environment` | 建置版本與執行環境中繼資訊（恆為 1） |

### 3.3 推薦 PromQL 告警規則

```promql
# 1. API 5xx 錯誤率 > 2%（持續 5 分鐘）
sum(rate(avelis_http_requests_total{status=~"5.."}[5m])) / clamp_min(sum(rate(avelis_http_requests_total[5m])), 1e-6) > 0.02

# 2. 問數 P95 延遲 > 10s
histogram_quantile(0.95, sum by (le) (rate(avelis_ask_duration_seconds_bucket[5m]))) > 10

# 3. LLM 呼叫失敗率 > 10%
sum(rate(avelis_llm_calls_total{status!="ok"}[10m])) / clamp_min(sum(rate(avelis_llm_calls_total[10m])), 1e-6) > 0.1

# 4. 中繼資料庫連線池借出耗盡（checked_out >= size）
avelis_db_pool_connections{state="checked_out"} >= on() group_left() avelis_db_pool_connections{state="size"}
```

---

## 4. 可選 OpenTelemetry 匯出

如需接入 Jaeger / Tempo / Datadog APM，可在安裝 `opentelemetry-sdk`、`opentelemetry-instrumentation-fastapi`、`opentelemetry-exporter-otlp` 後開啟：

```env
AVELIS_OTEL_ENABLED=true
AVELIS_OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318/v1/traces
```

未安裝上述可選依賴時，應用程式僅輸出一條警告日誌並降級為內建 TraceID + Prometheus 指標模式，不影響主流程啟動。