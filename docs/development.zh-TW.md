# 開發指南

[English](development.en.md) | [简体中文](development.md) | 繁體中文

本文面向在本機開發、除錯與驗收 Avelis 的開發者：後端與前端的開發命令、本地示範環境、一條命令復現驗收。部署到伺服器見 [deployment.md](deployment.zh-TW.md)，設定項見 [configuration.md](configuration.zh-TW.md)。

## 前置條件

- [uv](https://docs.astral.sh/uv/)（自動使用 Python 3.12）。Windows 上不要用裸 `python`，統一用 `uv run`。
- Node.js 24 與 pnpm 11。
- Docker Desktop / Docker Engine（Compose v2，為示範容器預留約 7 GB 記憶體）。

開發環境需要聯網安裝依賴（`uv sync`、`pnpm install`）；部署用的 Docker 映像則在建置時裝好全部依賴，執行時不聯網下載，見 [deployment.md](deployment.zh-TW.md)。

開發時後端與前端分開執行：後端 `127.0.0.1:8000`，前端 Vite 開發伺服器 `127.0.0.1:5173` 把 `/api` 代理到後端。生產環境由後端單埠同時提供介面與前端，見 [deployment.md](deployment.zh-TW.md)。

## 後端

```bash
cd backend
uv sync
uv run python -m avelis.bootstrap migrate      # 遷移中繼資料庫（PostgreSQL）
uv run python -m avelis.bootstrap admin        # 建立自動化管理員 ops_admin 與本地超級管理員 admin（見下）
uv run uvicorn avelis.api.app:app --port 8000  # API；介面文件 http://127.0.0.1:8000/api/docs
uv run python -m avelis.jobs                   # 後臺 worker：採集任務、定時輪詢
uv run python -m avelis.config check           # 檢視生效設定與來源（密碼脫敏）
AVELIS_FRONTEND_DIR=frontend/dist uv run python -m avelis.server  # 按生產方式單埠執行（需先 pnpm build）
uv run pytest                                  # 單元測試
uvx ruff@0.17.3 check .                        # 程式碼檢查（規則在 pyproject.toml；儲存庫根目錄再跑一次檢查指令碼）
uv run pytest -m acceptance                    # 驗收（會重置示範庫與中繼資料庫）
uv run python -m avelis.bootstrap demo --reset # 示範走查：重置並經 API 建好使用者、業務域、資料來源，完成首次採集
```

- **認證**：OAuth2 密碼模式 + PyJWT 存取權杖（可在 Redis 吊銷），密碼用 pwdlib（Argon2）雜湊；資料來源密碼用 Fernet 加密儲存，介面永不回傳。登入失敗按「帳號 + 來源地址」限流鎖定（`AVELIS_LOGIN_MAX_FAILURES` / `AVELIS_LOGIN_LOCK_MINUTES`）；管理員建立或重置密碼的帳號首次登入必須修改；改密碼、重置、停用後舊會話立即失效。
- **角色**：管理員、維護者、複核者、查詢者，加上業務域成員（負責人 / 維護者 / 可查詢）。預設禁止自審，策略見 `/api/settings`。
- **問數**：LangGraph 代理，能多輪追問、結合上下文理解追問，會話狀態存在 PostgreSQL。模型只提交結構化意圖，SQL 由已發布口徑確定性生成並校驗。支援多個指標、排名與前 N、環比 / 同比，以及衍生指標（如客單價 = 實付銷售額 / 訂單數），見 [ADR 0005](decisions/0005-ask-expressiveness.md)（簡體中文）。未指定模型時用示範模型（規則匹配，答案中會標明）。
- **分析**：多步分析代理，用於「為什麼變了、什麼關係、該怎麼做」這類問題。
  - 取數只走受治理查詢，計算由後端完成；
  - 報告中的數字逐個核驗，結論分事實、計算、推斷、建議；
  - 報告可匯出 PDF / Markdown / CSV，分析可儲存為排程任務。
  
  需要支援工具呼叫的真實模型，見 [ADR 0007](decisions/0007-analysis-agent.md)（簡體中文）。示範用的合成電商資料集用 `uv run scripts/ec_data.py load` 灌入，`bootstrap demo` 會自動接入。
- **模型設定**：在「設定 / 模型設定」頁面維護，支援 DeepSeek、OpenAI、Anthropic、通義千問、自定義 OpenAI 相容介面。金鑰加密儲存，只顯示末四位；檢測會驗證工具呼叫；按用途指定模型；預設向模型傳送查詢結果與脫敏後的程式碼片段（分析需要），可在同一頁面關閉，需保密時接入本地模型。開發時也可用 `uv run python -m avelis.bootstrap model` 把 `.env` 的 `AVELIS_MODEL_*` 登記進去。
- **匯入血緣**：上傳 SQL 檔案、貼上 SQL、上傳 Python / Shell 程式碼。解析出的邊直接寫入（可修改）；解析不了的片段由模型提出候選，必須人工接受、修改或駁回。
- **資料庫引擎**：每種資料庫是 `avelis.engines` 中的一個配接器（聯結器、SQL 方言、資產名層級、能力宣告），核心模組不按資料庫型別分支，見 `GET /api/engines`。目前登記：StarRocks、PostgreSQL、ClickHouse、SQL Server、Oracle、Doris（採集、檢視表血緣、問數、執行前核對），以及 MySQL（採集，作為外部表的追溯目標）。更多引擎的示範庫用 `uv run scripts/demo.py up --engines` 啟動。新增資料庫的步驟見 [ADR 0004](decisions/0004-engine-adapters.md)（簡體中文）。
- **執行前核對**：每次問數執行前（含命中快取）核對來源的列與檢視表定義是否與上次採集一致；不一致就攔截並排隊採集（`AVELIS_ASK_LIVE_CHECK`，預設開啟）。
- **欄位含義**：以資料庫列註釋為準，Avelis 不另設顯示名。問數與資產原樣展示列名和註釋，缺註釋如實標出；資料來源詳情給出註釋完整度與缺口清單（可匯出），發布檢查提示缺註釋（`AVELIS_REQUIRE_COLUMN_COMMENTS=true` 時阻斷），見 [ADR 0006](decisions/0006-column-meaning-from-db-comments.md)（簡體中文）。
- **權限**：資產按業務域可見；未歸屬業務域的資產預設只對管理員與維護者可見（`AVELIS_UNASSIGNED_ASSET_POLICY=deny|allow`）。
- **worker**：`AVELIS_WORKER_SCHEDULE_SECONDS`（輪詢，預設 60）、`AVELIS_WORKER_STALE_SECONDS`（未入隊任務的掃描閾值，預設 30）、`AVELIS_WORKER_RUNNING_TIMEOUT_SECONDS`（執行超時記為失敗，預設 3600）。
- 設計決定見 [ADR 0002](decisions/0002-backend-foundations.md)（簡體中文）、[ADR 0003](decisions/0003-postgres-agent-model-settings.md)（簡體中文）、[ADR 0004](decisions/0004-engine-adapters.md)（簡體中文）。

單元測試不連線任何外部服務，可以離線執行（用一個假的金鑰，不讀 `.env`）：

```bash
cd backend
AVELIS_SECRET=offline-unit-test-secret-0123456789 AVELIS_ENV_FILE=/nonexistent uv run --frozen python -m pytest tests/unit -q -p no:cacheprovider
```

## 前端

帳號：
- `admin`：本地超級管理員，擁有全部角色，密碼是 `.env` 中的 `AVELIS_ADMIN_PASSWORD`。只在中繼資料庫位於本機時建立，不經過密碼策略校驗；`bootstrap admin` 會把它重設為設定值。
- `ops_admin`：自動化管理員，示範準備與驗收指令碼用。
- 示範帳號：zhang（交易域負責人）、zhao（商品域負責人）、li（複核者）、wang、sun（分析師）。

`ops_admin` 與示範帳號的密碼都是 `.env` 中的 `AVELIS_DEMO_USER_PASSWORD`。

```bash
cd frontend
pnpm install
pnpm dev            # http://127.0.0.1:5173，/api 代理到 127.0.0.1:8000
pnpm gen:api        # 後端介面變化後重新生成型別
pnpm gen:tokens     # 設計權杖變化後重新生成 tokens.css
```

樣式全部來自 Avelis UI 設計系統：`frontend/design/tokens.json` 生成 `src/styles/tokens.css`，元件樣式在 `src/styles/avelis-ui.css`。

### 介面控制項

帶彈出層的控制項一律用 `src/components/controls.tsx` 裡的元件（從 `components/ui` 匯入），不用瀏覽器原生控制項。原生彈出層由作業系統繪製，在深色玻璃介面裡會變成白底或系統風格；問數輸入框和變更記錄都出過這個問題。

| 需要 | 用 | 不用 |
|---|---|---|
| 下拉選擇 | `Select`（子元素照常寫 `<option value>`，只當資料讀取） | `<select>` |
| 日期 | `DateInput`（值是 `YYYY-MM-DD`） | `type="date"` 等日期時間輸入 |
| 時間 | 兩個 `Select`（時、分） | `type="time"` |
| 可自由輸入、帶候選 | `SuggestInput` | `<datalist>` / `list=` |
| 二次確認 | `ConfirmDialog` | `confirm()` / `alert()` / `prompt()` |
| 選項少、要一眼看全 | `Seg` / `Tabs` | — |

這些浮層掛到 `body` 並依觸發器位置定位，面板的捲動和裁切不會遮住它們；鍵盤可操作（方向鍵、Enter、Esc、首字母跳轉）。自己寫的彈出選單若要判斷「點到外面」，要把 `.av-float-root` 裡的點擊算作裡面。

`pnpm check:ui` 檢查原始碼裡沒有上表「不用」一欄的寫法，CI 裡同樣會跑。

## 示範環境

只用於本地開發與驗收，所有埠只綁 `127.0.0.1`，資料全部是固定日期的合成電商資料：

- **StarRocks 3.5.14**（單容器）：交易事實表 `shop.orders`、`shop.order_items`、`shop.refund_raw`，檢視表 `shop.daily_sales`，分析檢視表 `ads.*`。
- **電商維表庫 MySQL 8.4**：`shop_dim` 中的客戶、商品、類目、區域。StarRocks 用兩種方式存取：JDBC Catalog `shop_mysql`，以及舊式 `ENGINE=mysql` 外部表 `dim.ext_region`。
- **Avelis 中繼資料庫**：PostgreSQL 18（也存放問數代理的會話狀態）。
- **Valkey 8**（Redis 協議）：任務佇列、快取、採集鎖。

### 資源要求

- Docker Desktop（為容器預留約 7 GB 記憶體；StarRocks 容器上限 5.25 GB）
- [uv](https://docs.astral.sh/uv/)（自動使用 Python 3.12）

### 常用命令

```bash
uv sync
uv run scripts/demo.py up        # 生成 .env、校驗 JDBC 驅動、啟動、等待就緒、灌數並核對基線
uv run scripts/demo.py check     # 再次核對驗收基線
uv run scripts/demo.py seed      # 把示範庫重置到基線
uv run scripts/demo.py verify    # StarRocks 實機核驗，結果寫入 .demo/sr-verify.json
uv run scripts/demo.py down      # 停止；加 --volumes 同時刪除資料
```

worker 長時間執行驗證（會臨時把採集間隔改為 1 分鐘，期間重啟 Valkey、修改一次檢視表，結束後恢復）：

```bash
cd backend && uv run python ../scripts/worker_soak.py --minutes 60
```

### 埠與帳號

| 服務 | 地址 | 帳號 |
|---|---|---|
| StarRocks（MySQL 協議） | `127.0.0.1:19030` | `root`（只用於灌數）、`avelis_collector`（採集）、`avelis_reader`（唯讀查詢） |
| StarRocks FE HTTP | `127.0.0.1:18030` | — |
| 電商維表庫 MySQL | `127.0.0.1:13306`（容器網路內 `shop-mysql:3306`） | `avelis_collector`（唯讀中繼資料）、`sr_catalog`（JDBC Catalog）、`sr_legacy`（舊式外部表） |
| 中繼資料庫 PostgreSQL | `127.0.0.1:15432` | `avelis` |
| Valkey | `127.0.0.1:16379` | 密碼認證 |

所有密碼由 `demo.py init-env` 隨機生成並寫入 `.env`（不進儲存庫，也不會列印）。示範網路使用固定子網 `10.240.17.0/24`，與本機網路衝突時在 `.env` 修改 `AVELIS_DEMO_SUBNET`。

### 驗收基線（`demo.py check`，用唯讀帳號查詢）

| 項 | 期望 |
|---|---|
| `shop.daily_sales` 2026-09-15 華東（實付） | 300.00 |
| 同日同區退款後淨額 | 270.00 |
| 訂單直接關聯退款明細後求和（粒度放大） | 400.00 |
| 訂單直接關聯訂單明細後求和（粒度放大） | 400.00 |
| `ads.sales_by_category` 2026-09-15 廚具（關聯 MySQL 維表） | 270.00 |
| `ads.member_sales` 2026-09-15 gold（關聯 MySQL 維表） | 250.00 |
| `ads.region_board` 華東 2026-09-15（經舊式外部表） | 300.00 |
| Catalog 中商品維錶行數 | 4 |

## 一條命令復現驗收

前置條件：Docker（Compose v2，為容器預留約 7 GB 記憶體）、[uv](https://docs.astral.sh/uv/)、pnpm。

```bash
uv run scripts/acceptance.py --fresh     # 從零：刪除示範容器與資料卷後重建，再跑全部驗收
uv run scripts/acceptance.py             # 複用已啟動的示範容器
uv run scripts/acceptance.py --no-llm    # 不呼叫真實模型（不向模型服務傳送任何資料）
```

依次執行：

1. 生成 `.env`（密碼隨機生成、不列印）；
2. 校驗 JDBC 驅動，啟動容器，灌入基線並核對 8 項基線數值；
3. 遷移中繼資料庫；
4. 後端單元測試；
5. 驗收（故事 1–9 與異常場景，全部在真實 StarRocks / MySQL / PostgreSQL / Valkey 上執行）；
6. 前端型別檢查與建置；
7. 把示範環境恢復到故事 1 的起點。

報告寫入 `.demo/acceptance-report.md`。`.env` 設定了模型金鑰時會執行 2 項真實模型用例，問題與指標中繼資料會傳送到設定的模型服務。

## 設計

- 設計權杖：`frontend/design/tokens.json`，`pnpm gen:tokens` 生成 `frontend/src/styles/tokens.css`；元件樣式在 `frontend/src/styles/avelis-ui.css`。
- 品牌標記：當前使用的標記在 `frontend/public/favicon.svg` 與 `frontend/src/components/Logo.tsx`。
- 文件寫法見[文件規範](documentation-guide.zh-TW.md)。
