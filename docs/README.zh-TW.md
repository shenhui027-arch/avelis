# Avelis 文件

[English](README.en.md) | [简体中文](README.md) | 繁體中文

按讀者分組。README 見 [English](../README.en.md) / [简体中文](../README.md) / [繁體中文](../README.zh-TW.md)；寫文件前請先看[文件規範](documentation-guide.zh-TW.md)。

## 使用與維運

| 文件 | 內容 |
|---|---|
| [介面導覽](product-tour.zh-TW.md) | 登入、工作台、問數與分析會話、匯出、資料集、個人經驗、血緣、目錄、設定的截圖與說明 |
| [部署指南](deployment.zh-TW.md) | Docker Compose、外部 PostgreSQL / Redis、不用 Docker 部署、升級、備份、排障 |
| [設定參考](configuration.zh-TW.md) | 設定檔與全部 `AVELIS_*` 環境變數、預設值與是否必填 |

## 開發

| 文件 | 內容 |
|---|---|
| [開發指南](development.zh-TW.md) | 後端與前端開發命令、本地示範環境、單元測試、一條命令復現驗收 |
| [架構與治理說明](architecture.zh-TW.md) | 組成、資料模型、關鍵機制、治理規則、執行與已知邊界 |
| [引擎核驗記錄](engines/) | 各資料庫引擎的實機核驗：[StarRocks 3.5.14](engines/starrocks.md) |
| [文件規範](documentation-guide.zh-TW.md) | README 與文件的結構、語言、術語、連結與圖片寫法 |
| [貢獻指南](../CONTRIBUTING.md) | 開發環境、PR 流程、測試要求 |

## 設計決策（ADR）

架構決策記錄只保留簡體中文原文。

| 編號 | 決定 |
|---|---|
| [0001](decisions/0001-demo-environment.md) | 示範環境的組成與約束 |
| [0002](decisions/0002-backend-foundations.md) | 後端基礎選型與治理模型落地 |
| [0003](decisions/0003-postgres-agent-model-settings.md) | 中繼資料庫收斂為 PostgreSQL；問數改為 LangGraph 代理；模型在頁面設定 |
| [0004](decisions/0004-engine-adapters.md) | 引擎適配層：多資料庫血緣的擴充套件方式 |
| [0005](decisions/0005-ask-expressiveness.md) | 問數表達能力：多指標、排名、對比與衍生指標 |
| [0006](decisions/0006-column-meaning-from-db-comments.md) | 欄位含義以資料庫列註釋為準 |
| [0007](decisions/0007-analysis-agent.md) | 分析代理：模型負責多步思考，治理守住每個數的來源 |
| [0008](decisions/0008-history-integrity.md) | 變更留存：歸因到資料庫使用者、檢索匯出、防篡改與保留期 |
| [0009](decisions/0009-grounded-numbers-and-evaluation.md) | 報告數字按引用渲染、自動歸因、探索查詢與評測集 |
| [0010](decisions/0010-business-neutral-product.md) | 產品程式碼與具體業務解耦 |
| [0011](decisions/0011-production-scale-context.md) | 生產規模下的按需上下文 |
| [0012](decisions/0012-analysis-pipeline-system-assembled-context.md) | 分析流程改為「系統組裝上下文、模型少輪推理」 |
| [0013](decisions/0013-accuracy-from-governed-metadata.md) | 對話式確認與經驗學習 |

## 術語

| 術語 | 含義 |
|---|---|
| 資料來源 | 被 Avelis 採集與查詢的一個資料庫例項（StarRocks、PostgreSQL 等），分別設定採集帳號與唯讀查詢帳號 |
| 資產 | 資料來源中的表、檢視表、外部表等物件，按物理 ID 識別 |
| 中繼資料庫 | Avelis 自己的 PostgreSQL 庫，存放資產、血緣、口徑、審計與代理會話 |
| 血緣 | 資產與欄位之間的推導關係，來自檢視表解析、匯入的 SQL / 程式碼與人工確認 |
| 口徑（指標） | 業務上認可的計算定義，有版本與發布 / 複核狀態；問數只用已發布的口徑 |
| 業務域 | 資產與口徑的歸屬與權限單位，成員分負責人、維護者、可查詢 |
| 問數 | 用自然語言提問，由已發布口徑確定性生成 SQL 並唯讀執行，答案帶來源與依據 |
| 分析 | 多步分析代理，回答「為什麼變了、什麼關係、該怎麼做」，報告中的數字逐個核驗 |
| 經驗 | 分析中經使用者確認或糾正的規則、關聯等，受治理並在後續分析中召回 |
| 變更留存 | 中繼資料與血緣的歷史版本，只追加並封入雜湊鏈，可核驗、檢索與匯出 |
| worker | 後臺程序，執行採集、排程任務與歷史清理 |
