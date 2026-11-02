# 治理事件與 Webhook

治理事件是 [ADR 0018](decisions/0018-governance-events-and-webhooks.md) 規定的穩定對外契約。後端從稽核記錄投影事件，在同一交易中寫入符合訂閱條件的寄件匣，再由 worker 傳送通用 JSON。首批不提供企業微信、釘釘、飛書格式，也不提供前置事件或程序內第三方監聽器。

本文對應 [`EventType` 與 action 對應](../backend/src/avelis/events/__init__.py)、[訂閱介面](../backend/src/avelis/api/events.py) 和[投遞實作](../backend/src/avelis/events/delivery.py)。其他語言：[简体中文](events.md) · [English](events.en.md)。

## 事件類型與稽核對應

類型只新增，不改名。下表的 outcome 是正常觸發路徑的值；受失敗守衛保護的寫入操作發生例外時，仍使用該操作類型，但 outcome 為 `failure`。未對應的稽核 action 繼續留底，不產生外部事件；可訂閱的列舉值本身也對應同名事件，供明確記錄與失敗留底使用。`webhook.test` 不參與稽核投影，也不可訂閱。

| 事件類型 | 稽核 action | 正常 outcome | 觸發條件 |
|---|---|---|---|
| `metric_version.submitted` | `metric_version.submit` | `success` | 口徑版本成功提交複核。提交寫入操作受失敗守衛保護。 |
| `metric_version.published` | `metric_version.approve` | `success` | 口徑版本複核通過並發布。發布寫入操作受失敗守衛保護。 |
| `metric_version.rejected` | `metric_version.reject` | `success` | 口徑版本被駁回。駁回寫入操作受失敗守衛保護。 |
| `metric_version.affected` | `metric_version.affected` | `success` | 擷取變更分析使已發布口徑，或受其組成版本影響的衍生口徑進入受影響狀態。 |
| `metric_version.invalidated` | `metric_version.invalidated` | `success` | 複核以 `new_version` 或 `retired` 處理受影響口徑，舊版本失效。對應寫入操作受失敗守衛保護。 |
| `join_path.approved` | `join_path.decide` | `success` | 本次決定為 `approved`，且路徑最終狀態為 `approved`，即目前所需業務域全部批准。單一業務域批准但路徑尚未滿足條件時不發事件。批准寫入操作受失敗守衛保護。 |
| `join_path.affected` | `join_path.reapproval_required` | `success` | 路徑依據改變，既有批准被清除，需要按目前業務域重新批准。 |
| `collect.completed` | `collect` | `success` | 一次資料來源擷取成功結束。 |
| `collect.failed` | `collect.failed` | `failure` | 擷取拋出例外，或 worker 回收執行逾時的擷取工作。 |
| `collect.schema_changed` | `collect.schema_changed` | `success` | 擷取發現變更並建立變更集；detail 帶變更計數，不含 DDL 原文。 |
| `lineage_import.completed` | `import.completed` | `success` | 血緣匯入的確定性解析處理結束，批次狀態可能為 `done`、`needs_params` 或 `needs_review`；不表示全部邊已批准或已經發布。輔助模型失敗保留既有處理語義。 |
| `lineage_import.failed` | `import.failed`；同名 action 用於失敗守衛 | `failure` | 建立匯入、補參數或匯入背景工作拋出例外，或 worker 回收執行逾時的匯入工作。 |
| `attribution.unavailable` | `attribution.unavailable` | `success` | 擷取歸因不可用，且原因為 `no_permission` 或 `not_enabled`。這裡的 success 表示成功記錄了「歸因不可用」這個事實，不表示歸因成功。其他歸因不可用原因不產生此類型。 |
| `auth.login_failure_threshold` | `auth.login_failure_threshold` | `failure` | 按「使用者名稱 + 來源位址」計數，失敗次數首次達到設定閾值。鎖定期間後續請求不重複發閾值事件；帳號不存在時 object.id 可為 `null`。 |
| `webhook.test` | 無（直接入匣；操作稽核為 `webhook_subscription.test`） | `success` | 只由「傳送測試」產生，僅投遞給被測試的訂閱，不受其 event_types 或 outcomes 篩選。不可訂閱，接收方可依此類型忽略測試事件。 |

## 事件契約與去識別

請求主體只有以下契約欄位；所有時間均為 UTC。`id` 是穩定事件 ID，正常事件取稽核列 ID，同一事件投向多個訂閱時保持一致。`X-Avelis-Delivery` 是某個訂閱的寄件匣 ID，與事件 ID 不同。

```json
{
  "id": "event_001",
  "type": "metric_version.published",
  "outcome": "success",
  "at": "2026-11-03T08:00:00Z",
  "actor": {"id": "user_001", "name": "admin"},
  "object": {"type": "metric_version", "id": "version_001"},
  "detail": {"dependency_fingerprint": "example-fingerprint"}
}
```

| 欄位 | 含義 |
|---|---|
| `id` | 穩定字串，接收方據此去重。 |
| `type` | 上表的有限列舉值。 |
| `outcome` | `success` 或 `failure`，應與 type 一起判斷。 |
| `at` | 事件發生時間，帶 UTC 時區。 |
| `actor` | `{id, name}`；系統操作為字串 `"system"`。 |
| `object` | `{type, id, name?}`；無法確定物件 ID 時 id 為 `null`，目前稽核投影不補充 name。 |
| `detail` | 已移除敏感內容的結構化欄位，預設為 `{}`。 |
| `error` | 僅失敗時必需：`{code, reason}`。成功事件通常省略。 |

失敗範例中，reason 是伺服器允許的簡短安全原因，既不是原始例外本文，也不是資料庫或遠端回應的副本：

```json
{
  "id": "event_002",
  "type": "collect.failed",
  "outcome": "failure",
  "at": "2026-11-03T08:01:00Z",
  "actor": "system",
  "object": {"type": "datasource", "id": "source_001"},
  "detail": {"trigger": "scheduled"},
  "error": {"code": "CollectionFailed", "reason": "操作失败"}
}
```

稽核寫入會遞迴移除密碼、憑證、權杖、授權標頭、API key、SQL 原文、資料列等敏感鍵，辨識到敏感文字時替換為 `[redacted]`。外部事件另以允許清單投影 detail，只保留執行／批次／工作 ID、狀態、決定、計數、原因碼等結構化欄位；自由文字 note、reason、warnings、解析器輸出與來源端錯誤不對外傳送。訂閱不會擴大資料權限，也不傳送業務資料列或查詢結果。

## 交易、歷史與權限

成功稽核與符合訂閱條件的 event_outbox 在業務交易中一起提交，回滾時一起撤銷。失敗守衛應包在寫入操作或背景工作的業務交易及 commit 外層：先讓業務交易回滾，再以獨立短交易寫失敗稽核與符合條件的寄件匣。失敗稽核仍使用 [ADR 0008](decisions/0008-history-integrity.md) 的全域雜湊鏈。失敗事件寫入本身再失敗時，只記不含例外本文的日誌，並原樣重新拋出最初的業務例外，不改既有錯誤回應。只記錄首批相關寫入操作與背景工作的失敗，不記普通查詢失敗。

口徑提交被阻擋的 `submit_blocked` 路徑是上述業務回滾規則的明確例外：沿用原 API 行為，先提交檢查結果與阻擋稽核，再拋出原衝突例外；失敗事件仍在另一個短交易中寫入。

事件中繼資料復用 `audit_event.detail._governance` 儲存 type、outcome 與選用 error，不為 audit_event 新增欄位，也不回寫既有歷史列。新稽核內容按既有封鏈機制參與摘要，既有歷史列的雜湊定義保持不變。

訂閱、事件類型與寄件匣介面全部要求管理員權限，已驗證的非管理員回傳 `403`。既有 `GET /api/audit` 權限繼續適用：管理員可見全部稽核；非管理員須有物件可見權限，且始終過濾 `outcome=failure` 的稽核。未帶治理中繼資料的歷史稽核按 success 處理。訂閱建立、修改、刪除、輪替金鑰、測試與 dead 重投都會寫稽核，不記錄簽章金鑰。

## 投遞與簽章驗證

worker 在 jobs.py 的定時掃描中處理到期且訂閱啟用的 pending 項目。每次請求使用 httpx，逾時參數為 10 秒，不跟隨重新導向，不使用環境代理；這是 httpx 的各階段逾時語義，並非嚴格的 10 秒總經過時間上限。只有 `2xx` 算成功。失敗按 1、2、4、8……分鐘退避，預設間隔上限 60 分鐘、最多嘗試 8 次；達到上限後為 dead，可由管理員重投。重投保留事件 ID 與寄件匣 ID，將 attempts 歸零並改回 pending。

稽核比對訂閱時，按訂閱 ID 順序以 `FOR KEY SHARE` 讀取啟用的訂閱，防止並行刪除訂閱造成寄件匣外鍵失敗並回滾業務。投遞交易使用 `FOR NO KEY UPDATE SKIP LOCKED` 鎖住選中的列，逐筆提交：投遞 worker 與訂閱管理操作仍互斥，同時相容於業務交易的 KEY SHARE 鎖。本次鎖變更目前只有 PostgreSQL SQL 編譯與靜態檢查證據，尚未通過真實 PostgreSQL 並行驗證。

遠端已接收、本地提交前程序結束時仍可能重複傳送，因此語義為至少一次，接收方必須按事件 id 去重。不保證順序；需要順序時應結合 at 與業務物件版本判斷。投遞失敗不回滾已提交的業務或稽核。last_error 只存安全錯誤碼，例如 timeout、network_error、url_rejected 或 http_500，不儲存回應本文。

每次傳送帶以下請求標頭：

```text
Content-Type: application/json
X-Avelis-Event: metric_version.published
X-Avelis-Delivery: delivery_001
X-Avelis-Timestamp: 1793692800
X-Avelis-Signature: sha256=<hex digest>
```

簽章輸入為 `timestamp + "." + body`，其中 timestamp 是標頭裡的 Unix 秒數字串，body 是收到的原始請求位元組，不可先解析 JSON 再序列化。以下為接收方驗證範例；300 秒視窗是接收方的防重播建議，不是伺服器設定。仍須在持久儲存中記錄已處理的事件 ID，並讓去重與業務處理具原子性。

```python
import hashlib
import hmac
import time


def verify_webhook(secret: str, headers, raw_body: bytes) -> bool:
    timestamp = headers.get("X-Avelis-Timestamp", "")
    signature = headers.get("X-Avelis-Signature", "")
    try:
        issued_at = int(timestamp)
        timestamp_bytes = timestamp.encode("ascii")
        signature_bytes = signature.encode("ascii")
    except (ValueError, UnicodeEncodeError):
        return False
    if abs(int(time.time()) - issued_at) > 300:
        return False
    expected = (
        "sha256="
        + hmac.new(
            secret.encode("utf-8"),
            timestamp_bytes + b"." + raw_body,
            hashlib.sha256,
        ).hexdigest()
    )
    return hmac.compare_digest(expected.encode("ascii"), signature_bytes)
```

簽章金鑰由伺服器產生，只在建立或輪替回應中回傳一次；列表與修改回應不回傳金鑰。儲存呼叫與資料來源密碼相同的 encrypt_secret/decrypt_secret，以 AVELIS_SECRET 經 HKDF 衍生金鑰的 Fernet 加密。輪替不會改寫既有事件快照，未投遞或重試項目在投遞時使用訂閱目前的金鑰；接收端應協調切換。

## URL 與設定

URL 只接受 http/https，不接受 URL 帳號／密碼、片段、控制字元或無效連接埠。建立、修改、測試、重投及每次實際傳送都會驗證目標。預設要求 DNS 解析出的全部位址都是允許的公網位址，拒絕內網、回環、鏈路本地、多播、保留位址與受限 IPv6 轉換位址。實際連線固定使用本次已驗證的 IP，同時保留原 HTTP Host、TLS SNI 與憑證網域驗證，避免驗證後再次解析造成 DNS 重綁定。僅 ConnectError 或 ConnectTimeout 時嘗試其餘已驗證位址，並按已用時間扣減共用的 10 秒連線重試預算；讀取逾時、寫入例外或 HTTP 失敗不切換位址，交給寄件匣後續重試。

| 環境變數 | 設定檔鍵 | 預設值 | 作用 |
|---|---|---|---|
| `AVELIS_WEBHOOK_ALLOW_PRIVATE_URLS` | `webhook.allow_private_urls` | `false` | 本地開發可明確允許內網位址；正式環境預設保持關閉。 |
| `AVELIS_WEBHOOK_MAX_ATTEMPTS` | `webhook.max_attempts` | `8` | 單輪投遞的最大嘗試次數，必須為正整數。 |
| `AVELIS_WEBHOOK_RETRY_MAX_MINUTES` | `webhook.retry_max_minutes` | `60` | 指數退避的分鐘上限，必須為正整數。 |
| `AVELIS_WORKER_SCHEDULE_SECONDS` | `worker.schedule_seconds` | `60` | worker 定時掃描間隔；寄件匣實際處理時間也受掃描與其他工作耗時影響。 |

## 管理介面與範例

以下介面沿用既有 Bearer 身分驗證，全部要求管理員。範例 ID 與金鑰均為預留值。

| 方法與路徑 | 請求 | 成功回應 |
|---|---|---|
| `GET /api/events/types` | 無 | `200`，`[{"type":"metric_version.submitted"}, ...]`，僅回傳可訂閱的 14 種，不含 `webhook.test`。 |
| `GET /api/webhook-subscriptions` | 無 | `200`，訂閱陣列，不含 secret。 |
| `POST /api/webhook-subscriptions` | name、url、非空 event_types；選用 outcomes、enabled | `201`，訂閱物件與一次性 secret。 |
| `PATCH /api/webhook-subscriptions/{id}` | 部分更新同上欄位；outcomes=null 清除 outcome 過濾 | `200`，訂閱物件，不含 secret。 |
| `DELETE /api/webhook-subscriptions/{id}` | 無 | `204`，無回應主體；刪除該訂閱全部寄件匣項目。 |
| `POST /api/webhook-subscriptions/{id}/rotate-secret` | 無 | `200`，訂閱物件與一次性新 secret。 |
| `POST /api/webhook-subscriptions/{id}/test` | 無 | `202`，新建的 OutboxOut；非同步傳送，不代表遠端已收到。 |
| `GET /api/event-outbox` | 選用 subscription_id、status、limit、offset | `200`，分頁物件。status 為 pending/delivered/dead；limit 預設 50、範圍 1–200，offset 預設 0。 |
| `POST /api/event-outbox/{id}/redeliver` | 無 | `200`，重設後的 OutboxOut；僅 dead 可重投。 |

建立訂閱：

```http
POST /api/webhook-subscriptions
Authorization: Bearer <admin-access-token>
Content-Type: application/json

{
  "name": "Governance alerts",
  "url": "https://hooks.example.com/avelis",
  "event_types": ["metric_version.published", "collect.failed"],
  "outcomes": ["success", "failure"],
  "enabled": true
}
```

```json
{
  "id": "subscription_001",
  "name": "Governance alerts",
  "url": "https://hooks.example.com/avelis",
  "event_types": ["metric_version.published", "collect.failed"],
  "outcomes": ["success", "failure"],
  "enabled": true,
  "created_by": "user_001",
  "created_at": "2026-11-03T08:00:00Z",
  "updated_at": "2026-11-03T08:00:00Z",
  "secret": "<save-this-secret-once>"
}
```

event_types 非空且去重，outcomes 省略或為 null 表示不過濾結果。修改範例為 `PATCH /api/webhook-subscriptions/subscription_001`，請求 `{"enabled":false,"outcomes":null}`；回傳上述訂閱物件的新值，但無 secret。輪替介面回傳相同形狀且帶新 secret。訂閱不存在為 `404`，非法 URL 或欄位為 `422`，停用訂閱的測試或重投、非 dead 項目的重投為 `409`。

測試事件固定使用 `type="webhook.test"`、`outcome="success"` 與新的事件 ID，object 包含訂閱的 type、id 與 name，detail 為 `{"test":true}`。它不受 event_types 或 outcomes 篩選，只投遞給被測試的這一個訂閱；接收方可根據 type 忽略測試事件。建立或修改訂閱時，event_types 中包含 `webhook.test` 回傳 `422`。測試回應與重投回應都是單一寄件匣物件；列表以相同物件組成 items，例如 `GET /api/event-outbox?subscription_id=subscription_001&status=pending&limit=50&offset=0`：

```json
{
  "items": [{
    "id": "delivery_001",
    "event_id": "event_test_001",
    "subscription_id": "subscription_001",
    "payload": {
      "id": "event_test_001",
      "type": "webhook.test",
      "outcome": "success",
      "at": "2026-11-03T08:02:00Z",
      "actor": {"id":"user_001","name":"admin"},
      "object": {"type":"webhook_subscription","id":"subscription_001","name":"Governance alerts"},
      "detail": {"test":true},
      "error": null
    },
    "status": "pending",
    "attempts": 0,
    "next_attempt_at": "2026-11-03T08:02:00Z",
    "last_error": null,
    "created_at": "2026-11-03T08:02:00Z",
    "delivered_at": null
  }],
  "total": 1,
  "limit": 50,
  "offset": 0
}
```

## FINDINGS：遷移、生命週期與驗收邊界

- 新增資料表為 webhook_subscription 與 event_outbox。後者存事件 JSON 快照、訂閱 ID、狀態、次數、下次嘗試時間、安全錯誤碼與完成時間；`(subscription_id, event_id)` 唯一，避免同一訂閱重複入匣。event_id 不對 audit_event 建外鍵，稽核按保留期清理不會阻斷剩餘快照投遞。
- 遷移檔案為 `0020_governance_events`，目前 `down_revision = "0018_import_batches"`。PR #81 已使用 `0019_metric_ai_context`；合併前 Claude 必須按實際 head 調整銜接。本遷移不新增稽核欄位、不重算舊雜湊。
- 刪除訂閱會級聯刪除其全部寄件匣項目，包括 pending、delivered 與 dead；原稽核不刪除。若要暫停並保留投遞記錄，應修改 enabled=false。停用不會取消既有 pending 項目，重新啟用後可繼續投遞。
- 寄件匣儲存事件快照；目標 URL、啟用狀態與簽章金鑰在投遞時讀取目前訂閱。修改類型或 outcome 過濾只影響隨後入匣，不刪除既有快照；輪替金鑰也用於舊快照的後續嘗試。
- 失敗 error 只承諾安全錯誤碼與預定義短原因，不承諾提供原始例外診斷。歸因不可用不使擷取失敗；匯入輔助模型失敗也不改變既有確定性結果保留語義。
- 相容性變化：統一稽核過濾使新寫入的 `analysis.explore` 稽核不再包含原先記錄的 SQL 原文；舊回歸斷言原本要求 `log.sql`，現已按使用者明確的「SQL 不寫入稽核」約束調整。權限範圍內的探索執行與資料集證據保留原行為，稽核仍保留安全的列數計數及 `digest`；已封鏈歷史列不改寫。
- 驗證入口包括 `backend/tests/unit/test_governance_events*.py`、`test_governance_event_business.py`、`backend/tests/db/test_governance_events_migration.py`、完整 unit 測試、Ruff、業務中立檢查，以及 `cd frontend && pnpm gen:api && pnpm typecheck`。單元測試與靜態檢查不能取代真實 PostgreSQL 遷移／雜湊鏈、多 worker 列鎖或外部 HTTPS 接收端驗收；實際執行結果以本次交付報告為準，本文不預先宣稱通過。
