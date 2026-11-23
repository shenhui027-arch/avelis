# 路線圖

[English](roadmap.en.md) | [简体中文](roadmap.md) | 繁體中文

本文面向 Avelis 開發者與貢獻者，記錄基於 `v1.0.11` 基線已達成一致的五階段開發計劃（`M1` 至 `M5`）、嚴格執行順序、工程邊界與驗收標準。文中 `M1` 至 `M5` 全部能力目前均處於**計劃**狀態，不承諾具體發布日期；目前已實現能力與系統結構見[架構與治理說明](architecture.zh-TW.md)與[開發指南](development.zh-TW.md)。

## 執行順序與邊界約束

### 嚴格執行順序

五階段計劃必須依 `M1 → M2 → M3 → M4 → M5` 順序推進，**`M5` 僅在 `M1` 至 `M4` 全部完成並驗收通過後實施**：

1. **M1（認證安全底座與商業化登入頁重構，計劃）**：先補齊身分驗證、工作階段安全與權限隔離漏洞，並完成登入、改密與管理員使用者維運介面重構。
2. **M2（介面體驗、動效、可存取性與前端效能，計劃）**：在穩定認證外殼之上打通互動斷鏈、補齊 CRUD 閉環、修復 WCAG AA 對比度與鍵盤無障礙，並完成路由與語系包拆包最佳化。
3. **M3（代理能力躍遷，計劃）**：在完整互動鏈路之上推進混合檢索、業務域經驗共享、已核驗查詢庫、多模型 SSE 串流與成本帳本、評測提分與 MCP / 技能安全擴充。
4. **M4（資料安全合規、可觀測性與工程化底座，計劃）**：補齊全部 8 個聯結器的傳輸安全與唯讀隔離、金鑰輪換、欄位遮罩與列級權限、稽核驗真、結構化可觀測與資料庫整合 CI。
5. **M5（萬表級規模重構與多副本高可用，計劃，最後階段）**：在 `M1` 至 `M4` 全部驗收通過後實施，重構全域鎖雜湊鏈、萬表級增量採集與檢索索引、多副本並行 worker、跨副本 SSE、物件儲存與雲原生零停機部署。

### 硬邊界與合規約束

| 約束類別 | 具體要求 |
|---|---|
| 原創實作與授權合規 | 不得照抄現有開源專案的程式碼行，須保持原創實作；可參考解析邏輯與架構思路，並可引入無商業化使用法律風險的預建置程式庫或框架（如 MIT、Apache-2.0、BSD、ISC 等寬鬆商用授權） |
| 明確不做的整合與功能 | 不接入釘釘、飛書、企業微信私有 SDK；不實作 SAML 2.0；不實作 LDAP / AD 直連；不實作 SCIM 2.0；不實作自建 SMTP 發信找回密碼；不開發公有雲自助註冊或計費功能 |
| 事實與基線區分 | 目前 `v1.0.11` 基線行為（例如未設初始密碼時回退 `admin / 123456`、MCP 硬鎖單一固定協定版本、全域 `history_chain_head` 列鎖、本機技能目錄與 Docker Compose 部署）由各階段計劃逐步演進替代，不與已發布基線混淆 |

## 階段總覽

| 階段 | 主題 | 狀態 | 前置依賴 | 核心目標 |
|---|---|---|---|---|
| M1 | 認證安全底座與商業化登入頁重構 | 計劃 | `v1.0.11` 基線 | 清零鑑權與越權漏洞，遷移至 Cookie 驗證，落地標準 OIDC / 可選 Forward Auth / 本機 TOTP 2FA / PAT，完成登入與改密頁雙主題重構及管理員使用者維運 |
| M2 | 介面體驗、動效、可存取性與前端效能 | 計劃 | M1 | 打通跨頁跳轉、失敗重試與排程任務/資料來源 CRUD 閉環，修復全量權杖 WCAG AA 對比度與鍵盤/浮層無障礙，最佳化動效並削減主套件體積 ≥ 50% |
| M3 | 代理能力躍遷 | 計劃 | M2 | 落地混合檢索、域共享經驗與已核驗查詢庫，解除單模型綁定並實作 SSE 串流與費用帳本，`scripts/eval_analysis.py` 整體通過率 ≥ 70%，完善唯讀沙盒、MCP 協商與技能審核 |
| M4 | 資料安全合規、可觀測性與工程化底座 | 計劃 | M3 | 全 8 個聯結器支援 TLS / CA 與唯讀工作階段，落地 `MultiFernet` 雙金鑰輪換、PII 遮罩、RLS、稽核與雜湊鏈驗真 UI、JSON 日誌與 `/metrics`、Postgres + Valkey CI 及 `sqlglot` 收斂 |
| M5 | 萬表級規模重構與多副本高可用 | 計劃（最後階段） | M1–M4 全部完成並驗收 | 去除全域列鎖改為批次或分片雜湊鏈與游標串流清理，實作萬表級增量採集與 `pg_trgm` / GIN 索引、多槽位隔離 worker、跨副本 SSE Pub/Sub、物件儲存與 Helm 零停機部署 |

## M1：認證安全底座與商業化登入頁重構（計劃）

目前 `v1.0.11` 基線中，未設定 `initial_admin_password` 時預設使用 `admin / 123456` 且不強制首次改密，部分介面缺少狀態校驗或歸屬校驗。`M1` 計劃完成以下能力：

### 前端登入頁、改密頁與管理員使用者維運（計劃）

- **登入頁與改密頁視覺重構**：重構 `frontend/src/pages/Login.tsx`、`frontend/src/styles/login.css` 與 `frontend/src/pages/account/ChangePassword.tsx`。左側打造更具建築感與資料敘事張力的展示舞台，呈現「血緣溯源 → 口徑校驗 → 歸因結論」聯動預覽，並對淺色與深色雙主題的材質與層次對比進行精調；右側重構表單排版與聚焦微互動，修復空態主按鈕對比度偏淡問題，並統一改密頁與登入頁設計語言。
- **商業化登入能力**：支援動態單槽位企業 OIDC 登入按鈕（未設定時保持純帳密極簡態，支援設定關閉本機登入）；支援輕白標設定（`brand_name`、`brand_logo_url`、`login_subtitle`、`terms_url`、`privacy_url`）；提供 7 天「記住登入」、全站密碼框顯隱切換、即時密碼規則校驗列，以及清晰的錯誤與鎖定倒數回饋。
- **管理員使用者維運補齊**：在 `frontend/src/pages/settings/UsersPage.tsx` 補齊使用者搜尋、重設密碼產生一次性可複製臨時密碼彈窗、強制踢下線與 `create.isPending` 防重複提交；在 `frontend/src/pages/catalog/DomainDetail.tsx` 補齊移除成員按鈕；在 `frontend/src/layout/Shell.tsx` 依角色隱藏無權存取的設定標籤頁。

### 後端安全漏洞清零與輕量標準 SSO（計劃）

- **強制改密與越權修復**：在 `backend/src/avelis/api/deps.py` 強制校驗 `user.must_change_password`（僅放行 `/api/me`、`/api/me/password`、`/api/auth/logout`），封堵 `/oauth/consent` 繞過；在 `backend/src/avelis/bootstrap.py` 淘汰無感預設 `admin / 123456`，改為強制首次改密或在未設環境變數時產生一次性隨機密碼寫入啟動日誌；修復 `backend/src/avelis/api/datasources.py` 中 `GET /api/jobs/{job_id}` 的 IDOR 越權；在 `backend/src/avelis/api/catalog.py` 補齊 `DELETE /api/domains/{domain_id}/members/{user_id}`。
- **HTTP 安全標頭與生產文件開關**：在 `backend/src/avelis/api/app.py` 新增 HTTP 安全標頭中介軟體（CSP、`X-Frame-Options: DENY`、`X-Content-Type-Options: nosniff`、`Referrer-Policy`、條件 `HSTS`），並在生產模式下預設關閉 `/api/docs` 與 `/api/openapi.json`。
- **防暴力破解、Redis 容錯與 Cookie 驗證**：在 `backend/src/avelis/api/auth.py` 與 `backend/src/avelis/cache.py` 落地原子 Redis `MULTI/EXEC` 計數、帳號/IP 雙維度鎖定、恆定耗時防 Argon2 時序枚舉、使用者名稱大小寫歸一，以及 `/api/me/password` 限流；鑑權路徑捕獲 `RedisError` 防止單點閃斷引發全站 500。驗證載體遷移至 `HttpOnly + Secure + SameSite=Lax` Cookie（涵蓋 `frontend/src/api/client.ts` 與後端驗證端點），同時保留 Bearer 權杖相容。
- **輕量標準驗證擴充**：僅實作 1 套通用標準 OIDC（Authorization Code + PKCE + 自動建號 JIT，直接複用 `backend/pyproject.toml` 已安裝的 `authlib`、`pyjwt`、`httpx`），加上可選約 20 行反向代理受信任請求標頭（Forward Auth）、可選純本機 TOTP 雙因素驗證與個人存取權杖（PAT）。

### M1 驗收重點（計劃）

- 後端安全測試涵蓋並全部通過上述漏洞修復、防暴力破解、Redis 容錯與 Cookie/Bearer 雙棧驗證，以及 OIDC / Forward Auth / TOTP / PAT 與管理員維運介面。
- 獨立評審代理依據視覺與互動評分表，對登入頁與改密頁在淺色/深色雙主題及桌面端/行動端 Playwright 截圖進行評審並通過。

## M2：介面體驗、動效、可存取性與前端效能（計劃）

`M2` 聚焦於打通前端互動斷鏈、補齊治理管理閉環、修復全站 WCAG AA 對比度與鍵盤無障礙，並顯著降低首屏套件體積與算繪回流。

### 互動斷鏈、死胡同與 CRUD 閉環修復（計劃）

- **跨頁跳轉、重試與匯出閉環**：修復 `frontend/src/pages/catalog/MetricDetail.tsx` 跳轉 `/ask?metric_id=...` 丟參數斷鏈並在 `/analysis` 讀取預置上下文，統一並收斂舊版 `/ask/:threadId` 與 `/analysis` 入口；在 `frontend/src/pages/analysis/AnalysisPage.tsx` 的失敗與停止態補齊一鍵重試和編輯重發，為同檔案中的 `LookupAnswer` 補齊匯出、複製 SQL 與「深入分析」入口。
- **全域元件與用戶端健壯性**：為 `frontend/src/components/ui.tsx` 的全域 `Code` 元件補齊一鍵複製 SQL；統一提問與追問提交快速鍵；修復 `frontend/src/api/client.ts` 中 `logout()` 缺少 `try/finally` 的清理缺陷，收斂 5 處裸 `fetch()` 呼叫，透傳 React Query `AbortSignal`，增加局部 `ErrorBoundary` 並修復 `RouteError`。
- **導覽效率與工作台體驗**：新增 `⌘K / Ctrl+K` 全域命令面板；為 `frontend/src/pages/Workbench.tsx` 增加冷啟動新手嚮導；補齊路由級 `document.title`、頁面麵包屑與側欄多資料來源異常彙總。
- **管理 CRUD 與細節打磨**：補齊排程任務（編輯、刪除、執行歷史、保留模型與技能、依列獨立 loading）與資料來源（編輯、刪除、測試連線）完整 CRUD；`frontend/src/pages/changes/ChangesPage.tsx` 展示全部 `definition_changed` 變更對照；`frontend/src/pages/changes/ChangeRecords.tsx` 補齊篩選重設與 URL 狀態同步；分析證據欄 `D3–D8` 區分維度標籤、血緣邊標籤防重疊（`frontend/src/pages/lineage/LineagePage.tsx`）、側欄最近對話漸層與「檢視全部」、技能卡人類可讀標題；修復 `frontend/src/numbers.ts` 在英文與繁中下輸出簡體「萬/億」以及 `frontend/src/pages/ask/presentation.ts` 僅匹配簡中表頭的正則缺陷。

### 設計權杖對比度、無障礙與動效效能最佳化（計劃）

- **設計系統與 WCAG AA 對比度**：修復 `frontend/design/tokens.json` 淺色/深色語意權杖的 WCAG AA 對比度（涵蓋 `--ok` 與 `--ok-subtle`、`--warn`、`--accent-text`、`--danger`、`--text-tertiary` 等），並擴充 `frontend/src/styles/contrast.test.mjs` 校驗全量語意權杖。
- **鍵盤與螢幕閱讀器無障礙（A11y）**：實作統一浮層棧以修復 `Drawer` 與 `ConfirmDialog`/`Select` 同時開啟時按 `Esc` 雙層關閉的缺陷；`ConfirmDialog` 危險操作預設聚焦取消按鈕並補齊 `aria-describedby`；修復 `DateInput` Tab 焦點陷阱與 ARIA grid 語意並支援年月快選或輸入；選單補齊方向鍵導覽與焦點歸還；`Banner` 補齊 `role="alert"` / `role="status"`；新增 Skip Link 與全域 `:focus-visible`。
- **動效收斂與前端效能**：解決 `frontend/src/styles/app.css` 與 `frontend/src/styles/pages.css` 中 `@keyframes av-shimmer` 同名覆蓋衝突；替換會觸發 `width`、`flex-grow` 或 `background-position` 版面重繪的動畫；節流最佳化 `frontend/src/pages/analysis/RunTranscript.tsx` 的 `useReveal` 以避免 60fps 回流；移除工作台三層疊加重複動效；修復 `Tooltip` 首幀方向偏差與 `prefers-reduced-motion` 漏網項；在 `frontend/src/main.tsx` 實施路由級 `React.lazy` 拆包與語系包按需載入（將約 1.21 MB 主套件體積削減 ≥ 50%）；在 `LineagePage.tsx` 拆分 `dagre.layout` 計算與選取態；停止側欄對已封存對話的全域輪詢；修復窄螢幕 `.av-fill` 捲動裁切並將 `100vh` 替換為 `100dvh`。

### M2 驗收重點（計劃）

- 擴充後的 `contrast.test.mjs` 涵蓋全量淺/深色語意權杖並全部通過 WCAG AA 校驗。
- 前端單測與 Playwright E2E 涵蓋並驗證全部互動閉環、CRUD、i18n 與鍵盤無障礙規則；`vite build` 主套件體積較 1.21 MB 削減 ≥ 50%。

## M3：代理能力躍遷（計劃）

`M3` 計劃全面升級問數與分析代理的檢索精度、多模型生態、串流互動、成本治理、評測得分與 MCP / 技能安全。

### 混合檢索與組織級知識飛輪（計劃）

- **混合檢索與已核驗查詢庫（VQR）**：在 `backend/src/avelis/ask/service.py`、`backend/src/avelis/analysis/tools.py` 與 `backend/src/avelis/analysis/retrieval.py` 落地語意向量與詞法回退相結合的混合檢索；新增 Golden SQL / Verified Query Repository（VQR）。
- **域共享經驗與問數閉環**：將 `backend/src/avelis/governance/experiences.py` 的個人經驗升級為支援團隊與業務域複核共享（含禁止自審）；問數模式補齊讚/踩回饋、長對話壓縮、解讀數字與查詢結果核驗，以及可選語意快取。

### 多模型生態、即時串流與成本帳本（計劃）

- **解耦模型協定與故障降級**：解除 `backend/src/avelis/modelcfg.py` 與 `backend/src/avelis/llm_protocol.py` 對 DeepSeek 的單一綁定，統一適配 OpenAI、Anthropic 及相容介面的串流推理與思考強度控制；支援主備模型自動降級與依任務複雜度路由。
- **全鏈路 SSE 串流與用量帳本**：前後端為問數與分析建立統一 SSE 即時事件流；補齊涵蓋問數與分析的全量 token 與費用帳本、單價設定以及依使用者/業務域的配額與限流。

### 分析深度擴充、評測達標與 MCP / 技能完善（計劃）

- **評測體系與 ≥ 70% 通過率目標**：基於 `scripts/eval_analysis.py` 重點攻克探索題（基線 12.5%）與負樣本題（基線 25.0%），將分析整體通過率提升至 ≥ 70%，並補充問數評測集與數字核驗測試。
- **唯讀沙盒與多引擎探索查詢**：擴充 `backend/src/avelis/analysis/explore.py` 的 `query_sql` 支援全部查詢引擎（目前 `v1.0.11` 僅支援部分引擎）並支援視窗函式；在受控唯讀 Python / DuckDB 沙盒內完成複雜計算；在 `backend/src/avelis/analysis/agent.py` 支援唯讀工具並行執行。
- **MCP 協定協商與技能安全**：修復 `backend/src/avelis/mcp/auth.py` 硬鎖單一固定 `Mcp-Protocol-Version` 的限制，改為向下相容協商；增強 `backend/src/avelis/mcp/tools.py` 中 `ask_metric` 的多指標、排序與篩選取值對齊；在 `backend/src/avelis/skills/` 禁止管理員自審自己上傳的技能包，增加上傳限流與 Prompt 注入/敏感詞預檢。

## M4：資料安全合規、可觀測性與工程化底座（計劃）

`M4` 補齊企業生產環境所需的資料傳輸加密、隱私遮罩、合規稽核、可觀測性指標及資料庫整合測試流水線。

### 聯結器加固、金鑰輪換與資料遮罩（計劃）

- **全 8 個聯結器 TLS 與唯讀加固**：為 `backend/src/avelis/connectors/` 全部 8 個資料來源聯結器（StarRocks、Doris、MySQL、PostgreSQL、ClickHouse、SQL Server、Oracle、Hive）補齊 TLS/SSL 與 CA 憑證設定；為 StarRocks、Doris、ClickHouse 補齊工作階段級唯讀控制。
- **雙金鑰輪換與 PII / RLS 治理**：在 `backend/src/avelis/security.py` 與 `backend/src/avelis/bootstrap.py` 實作真正的 `MultiFernet` 雙金鑰輪換 CLI；支援資產欄位級 PII 標記並在介面展示與模型外發前遮罩；對齊 `send_results` 預設安全原則；支援基礎列級權限（RLS）表示式注入。

### 稽核擴充、雜湊鏈驗真與留存清理（計劃）

- **稽核上下文與匯出留痕**：為 `AuditEvent` 補齊 `client_ip`、`user_agent` 與 `request_id`；記錄答案檢視及 CSV / PDF / HTML 匯出稽核事件；擴充安全 Webhook 事件類型；上線前端管理員稽核日誌查詢與雜湊鏈驗真頁面。
- **合規清理與匿名化**：在 `backend/src/avelis/history/retention.py` 實作 `Answer` 與 `AnalysisDataset` 保留期自動清理與離職/註銷帳號匿名化。

### 可觀測性、CI 流水線與依賴收斂（計劃）

- **日誌、指標與鏈路追蹤**：落地結構化 JSON 日誌、Prometheus `/metrics` 指標端點與可選 OpenTelemetry 接入。
- **全量整合 CI 與依賴收斂**：在 `.github/workflows/ci.yml` 增加 PostgreSQL 與 Valkey 服務容器，自動執行 `backend/tests/db` 與 `backend/tests/metadb` 套件、Docker 冒煙測試、安全漏洞掃描與 Playwright E2E；收斂 `backend/pyproject.toml` 中的 `sqlglot` 個人 Fork 依賴。

## M5：萬表級規模重構與多副本高可用（計劃，最後階段）

> **前置門禁**：`M5` 必須嚴格在 `M1`、`M2`、`M3`、`M4` 全部完成並通過驗收之後實施。

目前 `v1.0.11` 採用單點全域 `history_chain_head` 列鎖、`LOCK TABLE` 清理、全量圖指紋重算以及單副本本機技能儲存。`M5` 計劃面向萬表級中繼資料規模與多副本高可用生產環境完成以下重構：

### 雜湊鏈與歷史清理擴充性（計劃）

- **無全域列鎖雜湊鏈**：將 `backend/src/avelis/history/integrity.py` 中逐列鎖全域單列 `history_chain_head FOR UPDATE` 的觸發器機制重構為批次/交易級 Merkle 錨定或分片雜湊鏈。
- **游標串流驗真與清理**：改造 `backend/src/avelis/history/retention.py` 的驗真與保留期清理邏輯為游標串流處理，移除 `LOCK TABLE` 阻塞鎖。

### 萬表級目錄採集與檢索（計劃）

- **增量採集與局部子圖重算**：實作中繼資料增量採集、批次 Upsert 與局部受影響子圖的血緣及校驗碼（指紋）重算，避免全量重算開銷。
- **全文與模糊檢索索引**：落地 PostgreSQL `pg_trgm` 目錄檢索索引與 `Answer` GIN 索引；最佳化 Apache Hive 批次中繼資料拉取與目標庫連線複用。

### 多副本高可用與雲原生部署（計劃）

- **多槽位隔離 worker 與跨副本 SSE**：在 `backend/src/avelis/jobs.py` 與 `backend/src/avelis/cache.py` 實作多並行槽位、佇列隔離 worker 與依實例 ID 心跳；消除同步阻塞 `?wait=true` 查詢，基於 Redis Pub/Sub 實作跨副本 SSE 即時推送。
- **物件儲存與 Helm 零停機部署**：為技能包與匯出產物增加 S3 / MinIO 物件儲存後端；提供 Kubernetes Helm Chart（`deploy/helm/`）、Pre-deploy Migration Job、`CREATE INDEX CONCURRENTLY` 零阻塞索引遷移，以及應用優雅停機時的資料庫連線池與 Redis 連線釋放。

## 持續演進方向（計劃）

除上述五個順序推進的工程階段外，以下已公布的演進方向將在對應階段或後續迭代中持續推進，同樣不承諾日期：

- **更多引擎的外部 Catalog 與物化檢視表**：持續擴充跨引擎外部表與物化檢視表的中繼資料採集和血緣解析。
- **問數表達能力增強**：支援衍生指標巢狀、按週 / 月時間粒度以及佔比計算。

## 驗收與參考

| 驗證資源或參考文件 | 作用 |
|---|---|
| [`frontend/design/tokens.json`](../frontend/design/tokens.json) 與 [`contrast.test.mjs`](../frontend/src/styles/contrast.test.mjs) | 淺色/深色全量語意權杖 WCAG AA 對比度校驗 |
| [`scripts/eval_analysis.py`](../scripts/eval_analysis.py) | 分析代理評測腳本（`M3` 整體通過率 ≥ 70% 基線門禁） |
| [`backend/tests/db`](../backend/tests/db) 與 [`backend/tests/metadb`](../backend/tests/metadb) | PostgreSQL 與中繼資料庫整合測試套件（`M4` 接入 CI 容器流水線） |
| [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) | 儲存庫程式碼規範、單測、建置及後續整合/安全/E2E 流水線定義 |
| [架構與治理說明](architecture.zh-TW.md) 與 [ADR 0001–0022](decisions/)（簡體中文） | 目前 `v1.0.11` 基線架構設計與歷史決策上下文 |
