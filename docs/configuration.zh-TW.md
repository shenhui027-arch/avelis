# 設定參考

[English](configuration.en.md) | [简体中文](configuration.md) | 繁體中文

Avelis 的設定可以寫在**設定檔**（TOML）裡，也可以用 **`AVELIS_*` 環境變數**或 **`.env` 檔案**設定。生產部署推薦設定檔；本地開發沿用儲存庫根目錄的 `.env`。

## 載入順序

同一項在多處出現時，按以下優先順序取值（高 → 低）：

1. 程序環境變數 `AVELIS_*`
2. `.env` 檔案：預設儲存庫根目錄的 `.env`，可用 `AVELIS_ENV_FILE` 指定（相對路徑按儲存庫根目錄解析）
3. 設定檔：`AVELIS_CONFIG` 指定的檔案；未指定時若存在 `/etc/avelis/avelis.toml` 則讀取
4. 內建預設值

- `AVELIS_CONFIG` 指定了但檔案不存在或無法解析，啟動失敗並說明原因。
- 設定檔裡出現未知的節或鍵（多半是拼寫錯誤）也會報錯，並提示最接近的合法鍵名。
- Docker 映像約定設定檔掛載到容器內的 `/etc/avelis/avelis.toml`，部署編排已設定好 `AVELIS_CONFIG`。
- 樣例檔案：[deploy/docker/avelis.example.toml](../deploy/docker/avelis.example.toml)（每項都有中文註釋與預設值）。

### 檢查設定

```bash
cd backend && uv run python -m avelis.config check                 # 本機
docker compose run --rm app python -m avelis.config check          # Docker 部署（在 deploy/docker 下執行）
```

輸出每一項的生效值與來源（環境變數 / .env / 設定檔 / 預設），密碼與金鑰顯示為 `******`。啟動時同樣會校驗：缺必填項或格式錯誤時列印中文說明（哪一項、當前來自哪裡、怎麼改）並以退出碼 2 退出，不列印堆疊。

## 敏感項

密碼與金鑰允許明文寫在設定檔裡，但請注意：

- 設定檔與 `.env` 都要限制讀取權限（例如 `chmod 640`，屬組為容器使用者 UID 10001 可讀），不要提交到程式碼儲存庫，不要貼進聊天或工單。
- `AVELIS_SECRET` 務必與中繼資料庫一起備份：丟失後已儲存的資料來源密碼與模型金鑰無法解密。
- 更推薦的做法是**檔案引用**：把密碼放在單獨的檔案裡（如 Docker / Kubernetes secrets），設定裡只寫路徑。

| 寫法 | 示例 |
|---|---|
| 設定檔裡的明文 | `[database]` `password = "…"` |
| 設定檔裡的檔案引用 | `[database]` `password_file = "/run/secrets/avelis_db_password"` |
| 環境變數明文 | `AVELIS_DB_PASSWORD=…` |
| 環境變數檔案引用 | `AVELIS_DB_PASSWORD_FILE=/run/secrets/avelis_db_password` |

支援檔案引用的項：`AVELIS_SECRET`、`AVELIS_DB_PASSWORD`、`AVELIS_REDIS_PASSWORD`、`AVELIS_INITIAL_ADMIN_PASSWORD`、`AVELIS_MODEL_API_KEY`、`AVELIS_DATABASE_URL`、`AVELIS_REDIS_URL`。同一層裡明文與檔案引用同時出現會報錯；不同層之間按[載入順序](#載入順序)的優先順序覆蓋（例如環境變數 `AVELIS_DB_PASSWORD_FILE` 覆蓋設定檔裡的明文）。

## 設定項

「設定檔」一列是 TOML 中的 `[節] 鍵`。「必填」指沒有可用預設值、必須由部署者填寫的項。

### 安全 `[security]`

| 設定檔 | 環境變數 | 預設值 | 說明 |
|---|---|---|---|
| `secret` | `AVELIS_SECRET` | —（**必填**） | 主金鑰，至少 16 個字元；加密資料來源密碼與模型金鑰、簽發登入權杖。上線後不要更換 |
| `access_token_minutes` | `AVELIS_ACCESS_TOKEN_MINUTES` | `120` | 單個登入憑證有效期（閒置超時），分鐘 |
| `session_max_hours` | `AVELIS_SESSION_MAX_HOURS` | `168` | 一次登入最長可續期時長，小時 |
| `login_max_failures` | `AVELIS_LOGIN_MAX_FAILURES` | `5` | 同一帳號 + 來源地址連續失敗多少次後鎖定 |
| `login_lock_minutes` | `AVELIS_LOGIN_LOCK_MINUTES` | `15` | 鎖定時長，分鐘 |
| `allow_self_review` | `AVELIS_ALLOW_SELF_REVIEW` | `false` | 是否允許提交人複核自己的變更 |

### 中繼資料庫 `[database]`

中繼資料庫只支援 PostgreSQL。連線方式三選一，優先順序從高到低：完整連線串 `url` → 分項（填了 `host` 時）→ 本地示範預設（`127.0.0.1` + `AVELIS_PG_PORT` / `AVELIS_PG_PASSWORD`，只用於開發）。

| 設定檔 | 環境變數 | 預設值 | 說明 |
|---|---|---|---|
| `url` | `AVELIS_DATABASE_URL` | 空 | 完整連線串 `postgresql+psycopg://使用者:密碼@主機:埠/庫名`；填寫後忽略分項 |
| `host` | `AVELIS_DB_HOST` | 空 | 主機；Docker 內建服務為 `postgres` |
| `port` | `AVELIS_DB_PORT` | `5432` | 埠 |
| `name` | `AVELIS_DB_NAME` | `avelis` | 庫名 |
| `user` | `AVELIS_DB_USER` | `avelis` | 帳號（需能在該庫建表與模式） |
| `password` | `AVELIS_DB_PASSWORD` | —（用分項時**必填**） | 密碼 |
| `sslmode` | `AVELIS_DB_SSLMODE` | 空（驅動預設 `prefer`） | `disable` / `allow` / `prefer` / `require` / `verify-ca` / `verify-full` |
| `pool_size` | `AVELIS_DB_POOL_SIZE` | `5` | 每個程序的連線池大小 |
| `max_overflow` | `AVELIS_DB_MAX_OVERFLOW` | `10` | 連線池臨時溢位上限 |
| `pool_recycle_seconds` | `AVELIS_DB_POOL_RECYCLE_SECONDS` | `1800` | 連線回收週期，秒 |

### Redis `[redis]`

Redis 協議服務（Redis、Valkey 均可）。優先順序：`url` → 分項（填了 `host` 時）→ 本地示範預設（`127.0.0.1` + `AVELIS_REDIS_PORT` / `AVELIS_REDIS_PASSWORD`）。

| 設定檔 | 環境變數 | 預設值 | 說明 |
|---|---|---|---|
| `url` | `AVELIS_REDIS_URL` | 空 | 完整連線串 `redis://:密碼@主機:埠/0` |
| `host` | `AVELIS_REDIS_HOST` | 空 | 主機；Docker 內建服務為 `redis` |
| `port` | `AVELIS_REDIS_PORT` | `6379`（未填 `host` 時的示範預設為 `16379`） | 埠 |
| `db` | `AVELIS_REDIS_DB` | `0` | 庫號 |
| `password` | `AVELIS_REDIS_PASSWORD` | 空 | 密碼（Docker 內建服務**必填**） |
| `ssl` | `AVELIS_REDIS_SSL` | `false` | 是否使用 TLS（`rediss://`） |

### 服務 `[server]`

| 設定檔 | 環境變數 | 預設值 | 說明 |
|---|---|---|---|
| `host` | `AVELIS_HOST` | `127.0.0.1`（映像內 `0.0.0.0`） | 監聽地址 |
| `port` | `AVELIS_PORT` | `8000` | 監聽埠（Docker 對外埠在 Compose 裡設定） |
| `workers` | `AVELIS_WORKERS` | `1` | 程序數（uvicorn workers） |
| `public_url` | `AVELIS_PUBLIC_URL` | 空 | 對外存取地址，如 `http://10.0.0.5:8080`；用於啟動日誌並加入跨域白名單 |
| `frontend_dir` | `AVELIS_FRONTEND_DIR` | 空（映像內 `/app/frontend/dist`） | 建置好的前端目錄；設定後同一埠託管前端，SPA 路由回退到 `index.html`。相對路徑按儲存庫根目錄解析 |
| `forwarded_allow_ips` | `AVELIS_FORWARDED_ALLOW_IPS` | `127.0.0.1` | 信任哪些代理傳來的 `X-Forwarded-For`（逗號分隔，`*` 為全部） |
| `log_level` | `AVELIS_LOG_LEVEL` | `info` | `critical` / `error` / `warning` / `info` / `debug` |
| `cors_origins` | `AVELIS_CORS_ORIGINS` | `http://127.0.0.1:5173`、`http://localhost:5173` | 跨域白名單（設定檔裡寫陣列，環境變數逗號分隔）；同一埠存取時不需要 |

### 初始化 `[bootstrap]`

| 設定檔 | 環境變數 | 預設值 | 說明 |
|---|---|---|---|
| `auto_migrate` | `AVELIS_AUTO_MIGRATE` | `false`（Docker 樣例為 `true`） | `avelis.server` 啟動時自動遷移中繼資料庫（PostgreSQL 諮詢鎖序列） |
| `auto_init_admin` | `AVELIS_AUTO_INIT_ADMIN` | `false`（Docker 樣例為 `true`） | 啟動時建立首個管理員（庫裡已有管理員時跳過） |
| `initial_admin_username` | `AVELIS_INITIAL_ADMIN_USERNAME` | `admin` | 首個管理員帳號 |
| `initial_admin_password` | `AVELIS_INITIAL_ADMIN_PASSWORD` | 空（首次啟動**必填**） | 初始密碼：≥ 12 個字元、不含帳號、至少 4 種不同字元；首次登入必須修改 |

### 業務 `[business]`

| 設定檔 | 環境變數 | 預設值 | 說明 |
|---|---|---|---|
| `timezone` | `AVELIS_BUSINESS_TIMEZONE` | `Asia/Shanghai` | 業務時區 |
| `unassigned_asset_policy` | `AVELIS_UNASSIGNED_ASSET_POLICY` | `deny` | 未歸屬業務域的資產：`deny`（只有管理員與維護者可見）/ `allow` |
| `require_column_comments` | `AVELIS_REQUIRE_COLUMN_COMMENTS` | `false` | 口徑依賴的列缺資料庫註釋時是否阻斷發布（否則只提示） |

### 問數與查詢 `[query]`

| 設定檔 | 環境變數 | 預設值 | 說明 |
|---|---|---|---|
| `timeout_seconds` | `AVELIS_QUERY_TIMEOUT_SECONDS` | `30` | 單條查詢超時，秒 |
| `mem_limit_bytes` | `AVELIS_QUERY_MEM_LIMIT_BYTES` | `2147483648` | 單條查詢記憶體上限（支援的引擎），位元組 |
| `max_rows` | `AVELIS_QUERY_MAX_ROWS` | `1000` | 單條查詢返回行數上限 |
| `answer_cache_seconds` | `AVELIS_ANSWER_CACHE_SECONDS` | `300` | 答案快取時長，秒 |
| `live_check` | `AVELIS_ASK_LIVE_CHECK` | `true` | 問數執行前核對來源結構是否與上次採集一致 |
| `max_tool_rounds` | `AVELIS_ASK_MAX_TOOL_ROUNDS` | `8` | 問數代理一次回答中最多的工具呼叫輪數 |

### 採集與後臺任務 `[collect]` `[worker]` `[history]` `[import]`

| 設定檔 | 環境變數 | 預設值 | 說明 |
|---|---|---|---|
| `[collect] interval_minutes` | `AVELIS_COLLECT_INTERVAL_MINUTES` | `15` | 新建資料來源的預設採集間隔，分鐘（每個資料來源可單獨修改） |
| `[collect] ddl_audit_slack_seconds` | `AVELIS_DDL_AUDIT_SLACK_SECONDS` | `5` | DDL 審計歸因視窗兩端的時鐘容差，秒 |
| `[worker] schedule_seconds` | `AVELIS_WORKER_SCHEDULE_SECONDS` | `60` | 定時掃描間隔，秒 |
| `[worker] stale_seconds` | `AVELIS_WORKER_STALE_SECONDS` | `30` | 排隊多久未執行的任務由掃描補執行，秒 |
| `[worker] running_timeout_seconds` | `AVELIS_WORKER_RUNNING_TIMEOUT_SECONDS` | `3600` | 執行超時記為失敗（不自動重跑），秒 |
| `[history] retention_days` | `AVELIS_HISTORY_RETENTION_DAYS` | `1095` | 變更留存保留天數，`0` 為不清理 |
| `[history] purge_interval_hours` | `AVELIS_HISTORY_PURGE_INTERVAL_HOURS` | `24` | 超期歷史清理間隔，小時 |
| `[history] export_max_rows` | `AVELIS_HISTORY_EXPORT_MAX_ROWS` | `50000` | 變更記錄一次匯出的行數上限 |
| `[import] max_bytes` | `AVELIS_IMPORT_MAX_BYTES` | `2097152` | 匯入血緣時單個檔案或貼上內容的上限，位元組 |

### 分析代理 `[analysis]`

每次分析的預算；到達任一上限後停止取數並收尾成報告。

| 設定檔 | 環境變數 | 預設值 | 說明 |
|---|---|---|---|
| `seconds` | `AVELIS_ANALYSIS_SECONDS` | `300` | 時長上限，秒（同步分析介面另留 120 秒收尾餘量） |
| `tool_calls` | `AVELIS_ANALYSIS_TOOL_CALLS` | `30` | 工具呼叫次數上限 |
| `query_limit` | `AVELIS_ANALYSIS_QUERY_LIMIT` | `20` | 查詢類工具共用的次數額度 |
| `explore_limit` | `AVELIS_ANALYSIS_EXPLORE_LIMIT` | `8` | 探索查詢次數上限 |
| `model_requests` | `AVELIS_ANALYSIS_MODEL_REQUESTS` | `16` | 模型請求次數上限（含失敗與重試） |
| `input_tokens` | `AVELIS_ANALYSIS_INPUT_TOKENS` | `552321` | 累計輸入 token 上限，`0` 為不限 |
| `output_tokens` | `AVELIS_ANALYSIS_OUTPUT_TOKENS` | `30716` | 累計輸出 token 上限（含推理），`0` 為不限 |
| `material_change_ratio` | `AVELIS_MATERIAL_CHANGE_RATIO` | `0.01` | 變化顯著性閾值（口徑未單獨設定時） |
| `retrieval` | `AVELIS_ANALYSIS_RETRIEVAL` | `false` | 中繼資料圖證據發現（實驗特性） |
| `investigation` | `AVELIS_ANALYSIS_INVESTIGATION` | `false` | 證據解釋（實驗特性，本階段只定義開關） |

### 模型 `[model]`

模型提供方與金鑰建議在系統的「設定 → 模型設定」頁面維護（金鑰加密存入中繼資料庫，按用途指定模型）。下面的 `provider` 等幾項只用於 `python -m avelis.bootstrap model` 預置一個提供方。

| 設定檔 | 環境變數 | 預設值 | 說明 |
|---|---|---|---|
| `timeout_seconds` | `AVELIS_MODEL_TIMEOUT_SECONDS` | `60` | 分析代理單次模型請求超時，秒 |
| `provider` | `AVELIS_MODEL_PROVIDER` | 空 | `openai`（含任何 OpenAI 相容介面）/ `anthropic` |
| `name` | `AVELIS_MODEL_NAME` | 空 | 模型名 |
| `base_url` | `AVELIS_MODEL_BASE_URL` | 空 | 介面地址 |
| `api_key` | `AVELIS_MODEL_API_KEY` | 空 | 金鑰 |
| `send_results` | `AVELIS_MODEL_SEND_RESULTS` | `false` | 預置時是否允許把查詢結果發給模型 |

### 只能用環境變數設定的項

| 環境變數 | 說明 |
|---|---|
| `AVELIS_CONFIG` | 設定檔路徑 |
| `AVELIS_ENV_FILE` | `.env` 檔案路徑 |
| `AVELIS_PDF_FONT` | 分析報告匯出 PDF 時嵌入的中文字型檔案（映像內已裝文泉驛微米黑，一般不需要） |
| `AVELIS_PG_PORT`、`AVELIS_PG_PASSWORD` | 本地示範中繼資料庫（未設定 `[database]` 時使用） |
| `AVELIS_DEMO_USER_PASSWORD`、`AVELIS_ADMIN_PASSWORD` | 本地示範帳號與本地超級管理員（只用於開發，見 [development.md](development.zh-TW.md)） |

## Docker 部署的編排引數

`deploy/docker/.env`（可選，模板 `.env.example`）只放 Compose 自身的引數，不放應用設定：

| 變數 | 預設值 | 說明 |
|---|---|---|
| `AVELIS_HTTP_PORT` | `8080` | 對外埠 |
| `AVELIS_BIND_ADDRESS` | `0.0.0.0` | 繫結地址；前面有反向代理時改為 `127.0.0.1` |
| `AVELIS_IMAGE` | `avelis:1.0.1` | 映像名 |
| `COMPOSE_FILE` | — | 使用外部 PostgreSQL / Redis 時設為 `compose.yaml:compose.external.yaml` |
