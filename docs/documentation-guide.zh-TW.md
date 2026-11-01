# 文件規範

[English](documentation-guide.en.md) | [简体中文](documentation-guide.md) | 繁體中文

這份規範約定 Avelis 儲存庫裡 README 與文件的結構、語言和寫法。它參考了 Apache Superset、DataHub、OpenMetadata、Airbyte、dbt、Metabase 等資料與基礎設施類開源專案的做法，只保留適合 Avelis 現階段的部分。

## 1. 參考專案的共同做法

| 做法 | 參考專案 | Avelis 採用方式 |
|---|---|---|
| 標題下方一行徽章（授權、版本、建置狀態、社群） | Superset、DataHub、Airbyte、Metabase | 只放真實存在的徽章：授權、CI、最新版本；沒有發布或社群渠道時不放對應徽章 |
| 一句話定位 + 一張產品截圖 | Metabase、DataHub | 標題下一句話說清「是什麼、給誰用」，緊跟一張真實截圖 |
| 功能亮點用短列表或帶標題的截圖 | Superset（截圖配加粗說明）、OpenMetadata | 4–6 條亮點，每條一句；截圖用表格兩列排布，配說明 |
| 支援的資料庫單列一節 | Superset、Metabase | 「支援的資料庫」表格，寫清每個引擎支援到哪一步 |
| 快速開始只給最短可執行路徑，細節鏈到文件 | DataHub、Airbyte、Metabase | 一段 Docker Compose 命令；外部資料庫、反向代理、升級等鏈到部署指南 |
| 文件按讀者分組：使用者 / 管理員 / 開發者 | Superset | `docs/README.md` 按「使用與維運 / 開發 / 設計決策 / 歸檔」分組 |
| 貢獻、行為準則、安全各有獨立檔案，README 只連結 | dbt、Airbyte、Metabase | `CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`、`SECURITY.md` 放儲存庫根目錄 |
| 安全問題不走公開 issue | Airbyte、Metabase | `SECURITY.md` 說明私下報告渠道 |
| 授權放在最後一節，一兩行 | DataHub、OpenMetadata | 同 |

## 2. 儲存庫根目錄檔案

| 檔案 | 語言 | 內容 |
|---|---|---|
| `README.md` | English | 專案入口，結構見第 3 節；GitHub 預設展示 |
| `README.zh-CN.md` | 簡體中文 | 與英文版同結構、同內容；**內容的準確來源** |
| `README.zh-TW.md` | 繁體中文 | 與簡體中文版同結構、同內容 |
| `CONTRIBUTING.md` | 三語分節 | 開發環境、提交規範、PR 流程、測試要求 |
| `CODE_OF_CONDUCT.md` | 三語分節 | 採用 Contributor Covenant 2.1 |
| `SECURITY.md` | 三語分節 | 支援的版本、私下報告方式、處理時限 |
| `CHANGELOG.md` | 簡體中文 | 按 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式記錄版本變化 |
| `LICENSE` | English | Apache License 2.0 原文，不改動 |
| `.github/` | 簡體中文 + English | Issue 模板（表單標籤雙語）、PR 模板、CI |

三份 README 改動時必須同步，規則見第 5 節。

## 3. README 結構

按以下順序，沒有內容的節不寫：

1. **標題與徽章**：`# Avelis`，下面一行徽章，再一行語言切換連結。
2. **一句話定位**：粗體一句，說清是什麼；再用一兩句說清解決什麼問題。
3. **主截圖**：一張最能代表產品的真實截圖。
4. **功能亮點**：短列表，每條以粗體關鍵詞開頭。
5. **截圖**：兩列表格，每張配一句說明；更多截圖鏈到介面導覽。
6. **支援的資料庫**：表格，列出每個引擎支援的能力。
7. **快速開始**：前置條件 + 最短可執行命令 + 開啟哪個地址、用什麼帳號。
8. **架構**：一張 Mermaid 圖 + 三五條要點，細節鏈到架構文件。
9. **設定**：必填項表格，全部設定鏈到設定參考。
10. **開發**：後端與前端各一段命令，細節鏈到開發指南。
11. **文件**：按讀者分組的連結。
12. **路線圖**：只寫已決定要做的方向，鏈到 issue 或里程碑；不承諾日期。
13. **參與貢獻**：鏈到 `CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`。
14. **安全**：鏈到 `SECURITY.md`。
15. **授權**：一行，鏈到 `LICENSE`。

## 4. docs 目錄

```
docs/
  README.md / README.en.md / README.zh-TW.md     文件索引（按讀者分組）
  product-tour.md (+ .en.md / .zh-TW.md)          使用者：介面導覽
  deployment.md (+ .en.md / .zh-TW.md)            維運：部署指南
  configuration.md (+ .en.md / .zh-TW.md)         維運：設定參考
  development.md (+ .en.md / .zh-TW.md)           開發者：開發指南
  architecture.md (+ .en.md / .zh-TW.md)          開發者：架構與治理說明
  documentation-guide.md (+ .en.md / .zh-TW.md)   開發者：本規範
  engines/                 開發者：各資料庫引擎的實機核驗記錄（只有簡體中文）
  decisions/               設計決策 ADR（只有簡體中文）
  images/                  README 與文件引用的截圖
  archive/                 內部記錄：開發計劃、里程碑驗收、實驗記錄（只有簡體中文，唯讀存檔）
```

- **使用者 / 維運文件**回答「怎麼用、怎麼部署、怎麼設定」，不寫實現細節和開發過程。
- **開發者文件**回答「程式碼怎麼組織、怎麼跑測試、為什麼這樣設計」。
- **ADR** 記錄一次設計決定的背景、選項與後果，合併後只追加「狀態」與「後續」，不改寫決定本身。
- **歸檔**是開發過程中的計劃、驗收流水與實驗記錄，保留作為可追溯的依據，不再維護；新文件不要連結到歸檔裡的過程細節，除非是作為證據引用。

## 5. 多語言

Avelis 的產品與文件支援三種語言：簡體中文（`zh-CN`）、繁體中文（`zh-TW`）、English（`en`）。

### 哪些文件翻譯

| 類別 | 語言 | 原因 |
|---|---|---|
| README、docs 索引、使用者 / 維運 / 開發者文件、本規範 | 三語 | 使用、部署、參與開發都需要讀懂 |
| `CONTRIBUTING`、`CODE_OF_CONDUCT`、`SECURITY` | 同一檔案內三語分節 | 篇幅短，放在一起便於同步 |
| ADR、引擎核驗記錄、歸檔、`CHANGELOG` | 只有簡體中文原文 | 是某一時刻的記錄，翻譯會造成多個「原文」；英文與繁中索引列出它們並註明「簡體中文」 |

### 檔案命名

docs 下用**同名檔案加語言字尾**：`deployment.md` 是簡體中文原文，`deployment.en.md` 是英文，`deployment.zh-TW.md` 是繁體中文。不按語言分目錄，原因是：

- 現有文件路徑、程式碼註釋與外部連結不變；
- 截圖按介面語言分目錄（見下方「截圖」），檔名三種語言相同；
- 評審時原文與譯文在同一目錄，容易對照；
- 與常見文件站點工具的「字尾式」多語言約定一致，以後建站不用搬檔案。

### 原文與同步

- **簡體中文是原文**，英文與繁中是譯文。README 例外：GitHub 預設展示英文版，但內容仍以簡體中文版為準。
- 改原文時在同一個 PR 裡同步更新兩份譯文。來不及翻譯時，在譯文語言切換行下面加一行提示「本文落後於簡體中文原文」，並開 issue 跟進。
- 繁中譯文先用 [OpenCC](https://github.com/BYVoid/OpenCC) 的 `s2twp`（臺灣用詞）轉換，再按下面的術語表逐項校對，不能只做字形轉換。
- 英文譯文按意思翻譯，不逐字對應；命令、設定項、程式碼與路徑保持原樣。

### 語言切換行

每份三語文件在一級標題下面放一行語言切換，當前語言不加連結，順序固定為 English、簡體中文、繁體中文：

```markdown
# 部署指南

[English](deployment.en.md) | 简体中文 | [繁體中文](deployment.zh-TW.md)
```

### 連結

- 譯文裡的連結指向同一語言的版本：`deployment.en.md` 連結 `configuration.en.md`。
- 目標沒有該語言版本時（ADR、引擎核驗、歸檔），連結原文並在連結文字後註明：英文寫 「(Simplified Chinese)」，繁中寫「（簡體中文）」。

### 截圖

- 截圖按介面語言分目錄：簡體中文放 `docs/images/`，英文放 `docs/images/en/`，繁中放 `docs/images/zh-TW/`，檔名相同；各語言文件引用自己語言的截圖。
- 替換截圖時三種語言一起重拍（同一份示範資料、1440×900），同名覆蓋。

### 術語對照

三種語言的譯法以本表為準；新增術語先加到這裡。「不寫」一列只列簡體中文裡容易混用的寫法。

| 簡體中文 | 繁體中文 | English | 不寫（簡體中文） |
|---|---|---|---|
| 数据源 | 資料來源 | data source | 数据连接、库连接 |
| 资产 | 資產 | asset | |
| 元数据 | 中繼資料 | metadata | |
| 元数据库 | 中繼資料庫 | metadata database | 系统库、Meta DB |
| 采集 | 採集 | collection / collect | 同步、抓取 |
| 采集账号 / 只读账号 | 採集帳號 / 唯讀帳號 | collector account / read-only account | |
| 血缘（表级 / 字段级） | 血緣（表級 / 欄位級） | lineage (table-level / column-level) | |
| 视图 | 檢視表 | view | |
| 外部表 | 外部表 | external table | |
| 口径（指标） | 口徑（指標） | metric definition (metric) | 度量、KPI |
| 派生指标 | 衍生指標 | derived metric | |
| 业务域 | 業務域 | business domain | 业务线、部门（作为权限单位时） |
| 关联路径 | 關聯路徑 | join path | |
| 粒度 | 粒度 | grain | |
| 发布 / 复核 | 發布 / 複核 | publish / review | |
| 变更管理 | 變更管理 | change management | |
| 变更留底 | 變更留存 | change history | 变更历史（作为功能名时） |
| 执行前核对 | 執行前核對 | pre-query schema check | |
| 问数 | 問數 | Ask (data Q&A) | 问答、Chat BI |
| 分析 / 分析智能体 | 分析 / 分析代理 | analysis / analysis agent | Agent 分析 |
| 智能体 | 代理 | agent | |
| 受治理查询 | 受治理查詢 | governed query | |
| 探索查询 | 探索查詢 | exploratory query | |
| 数据集 | 資料集 | dataset | |
| 报告（简报 / 完整版） | 報告（簡報 / 完整版） | report (brief / full) | |
| 经验 | 經驗 | experience | 记忆 |
| 定时任务 | 排程任務 | scheduled analysis | |
| 工作台 | 工作台 | workbench | |
| 目录 | 目錄 | catalog | |
| 模型 / 模型服务 | 模型 / 模型服務 | model / model provider | 大模型、LLM（正文中） |
| 配置 / 配置文件 | 設定 / 設定檔 | configuration / configuration file | |
| 数据库用户 | 資料庫使用者 | database user | |
| worker | worker | worker | |

## 6. 寫法

### 標題與結構

- 每個檔案只有一個一級標題 `#`，即文件標題；正文從 `##` 開始，最多用到 `####`。
- 標題用名詞短語，不加句號；編號只在步驟性文件裡用（「1. 準備」）。
- 開頭一兩句說明這篇文件給誰看、解決什麼問題。
- 一段只講一件事；能用表格對照的不用長段落。

### 語言

- 句子短，用主動語態；面向使用者的文件不用內部代號（M3、E1、P0、B0 等），需要時寫成功能名。
- 中文與英文、數字之間加一個空格：「支援 StarRocks 3.5」。
- 中文用全形標點；程式碼、命令、路徑、設定項用反引號。
- 不寫「簡單地」「只需」「顯然」；英文同理不寫 「simply」「just」「obviously」。
- 術語按第 5 節的對照表。

### 連結與圖片

- 儲存庫內連結一律用相對路徑：`[部署指南](deployment.zh-TW.md)`、`[ADR 0004](decisions/0004-engine-adapters.md)（簡體中文）`。
- 不寫本機絕對路徑（`E:\...`、`C:\Users\...`）、內部會話連結或私有工作區連結。
- 截圖放在 `docs/images/`，檔名用小寫英文與連字元，描述畫面內容：`analysis-dataset.png`。
- 截圖必須來自真實執行的產品與示範資料，不放設計稿冒充功能；替換截圖時同名覆蓋，避免連結失效。
- 圖片寫替代文字，用文件本身的語言：`![血緣：選中表與檢查器](images/lineage.png)`。

### 命令與程式碼塊

- 程式碼塊標註語言：`bash`、`powershell`、`toml`、`python`。
- 寫進文件的命令必須實際執行過；只在特定平臺成立的命令註明平臺。
- 不在命令前加 `$`，不把輸出混進命令塊；需要展示輸出時另起一個程式碼塊。
- 示例密碼、金鑰一律用明顯的佔位符（`change-me`、`<your-secret>`），不寫真實值。
- 譯文裡程式碼塊的註釋可以翻譯，命令本身不改。

### 事實與狀態

- 只寫已經實現並驗證過的功能；計劃中的寫進路線圖，並標明「計劃」。
- 寫驗證結論時寫清範圍：在什麼環境、什麼版本、跑了什麼。
- 已知侷限單獨列出，不藏在正文裡。

## 7. 提交與檢查

- 文件改動與程式碼改動可以在同一個 PR，但 README 與 `docs/` 的重組單獨提 PR。
- 提交前檢查：相對連結與圖片路徑能開啟；三份 README 與三語文件同步；沒有本機路徑與憑證。
- 提交說明與 PR 用簡體中文，見 [CONTRIBUTING.md](../CONTRIBUTING.md)。
