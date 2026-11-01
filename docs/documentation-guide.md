# 文档规范

[English](documentation-guide.en.md) | 简体中文 | [繁體中文](documentation-guide.zh-TW.md)

这份规范约定 Avelis 仓库里 README 与文档的结构、语言和写法。它参考了 Apache Superset、DataHub、OpenMetadata、Airbyte、dbt、Metabase 等数据与基础设施类开源项目的做法，只保留适合 Avelis 现阶段的部分。

## 1. 参考项目的共同做法

| 做法 | 参考项目 | Avelis 采用方式 |
|---|---|---|
| 标题下方一行徽章（许可、版本、构建状态、社区） | Superset、DataHub、Airbyte、Metabase | 只放真实存在的徽章：许可、CI、最新版本；没有发布或社区渠道时不放对应徽章 |
| 一句话定位 + 一张产品截图 | Metabase、DataHub | 标题下一句话说清“是什么、给谁用”，紧跟一张真实截图 |
| 功能亮点用短列表或带标题的截图 | Superset（截图配加粗说明）、OpenMetadata | 4–6 条亮点，每条一句；截图用表格两列排布，配说明 |
| 支持的数据库单列一节 | Superset、Metabase | “支持的数据库”表格，写清每个引擎支持到哪一步 |
| 快速开始只给最短可运行路径，细节链到文档 | DataHub、Airbyte、Metabase | 一段 Docker Compose 命令；外部数据库、反向代理、升级等链到部署指南 |
| 文档按读者分组：用户 / 管理员 / 开发者 | Superset | `docs/README.md` 按“使用与运维 / 开发 / 设计决策 / 归档”分组 |
| 贡献、行为准则、安全各有独立文件，README 只链接 | dbt、Airbyte、Metabase | `CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`、`SECURITY.md` 放仓库根目录 |
| 安全问题不走公开 issue | Airbyte、Metabase | `SECURITY.md` 说明私下报告渠道 |
| 许可放在最后一节，一两行 | DataHub、OpenMetadata | 同 |

## 2. 仓库根目录文件

| 文件 | 语言 | 内容 |
|---|---|---|
| `README.md` | 简体中文 | 项目入口，结构见第 3 节；GitHub 默认展示；**内容的准确来源** |
| `README.en.md` | English | 与简体中文版同结构、同内容 |
| `README.zh-TW.md` | 繁體中文 | 与简体中文版同结构、同内容 |
| `CONTRIBUTING.md` | 三语分节 | 开发环境、提交规范、PR 流程、测试要求 |
| `CODE_OF_CONDUCT.md` | 三语分节 | 采用 Contributor Covenant 2.1 |
| `SECURITY.md` | 三语分节 | 支持的版本、私下报告方式、处理时限 |
| `CHANGELOG.md` | 简体中文 | 按 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式记录版本变化 |
| `LICENSE` | English | Apache License 2.0 原文，不改动 |
| `.github/` | 简体中文 + English | Issue 模板（表单标签双语）、PR 模板、CI |

三份 README 改动时必须同步，规则见第 5 节。

## 3. README 结构

按以下顺序，没有内容的节不写：

1. **标题与徽章**：`# Avelis`，下面一行徽章，再一行语言切换链接。
2. **一句话定位**：粗体一句，说清是什么；再用一两句说清解决什么问题。
3. **主截图**：一张最能代表产品的真实截图。
4. **功能亮点**：短列表，每条以粗体关键词开头。
5. **截图**：两列表格，每张配一句说明；更多截图链到界面导览。
6. **支持的数据库**：表格，列出每个引擎支持的能力。
7. **快速开始**：前置条件 + 最短可运行命令 + 打开哪个地址、用什么账号。
8. **架构**：一张 Mermaid 图 + 三五条要点，细节链到架构文档。
9. **配置**：必填项表格，全部配置链到配置参考。
10. **开发**：后端与前端各一段命令，细节链到开发指南。
11. **文档**：按读者分组的链接。
12. **路线图**：只写已决定要做的方向，链到 issue 或里程碑；不承诺日期。
13. **参与贡献**：链到 `CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`。
14. **安全**：链到 `SECURITY.md`。
15. **许可**：一行，链到 `LICENSE`。

## 4. docs 目录

```
docs/
  README.md / README.en.md / README.zh-TW.md     文档索引（按读者分组）
  product-tour.md (+ .en.md / .zh-TW.md)          用户：界面导览
  deployment.md (+ .en.md / .zh-TW.md)            运维：部署指南
  configuration.md (+ .en.md / .zh-TW.md)         运维：配置参考
  development.md (+ .en.md / .zh-TW.md)           开发者：开发指南
  architecture.md (+ .en.md / .zh-TW.md)          开发者：架构与治理说明
  documentation-guide.md (+ .en.md / .zh-TW.md)   开发者：本规范
  engines/                 开发者：各数据库引擎的实机核验记录（只有简体中文）
  decisions/               设计决策 ADR（只有简体中文）
  images/                  README 与文档引用的截图
  archive/                 内部记录：开发计划、里程碑验收、实验记录（只有简体中文，只读存档）
```

- **用户 / 运维文档**回答“怎么用、怎么部署、怎么配置”，不写实现细节和开发过程。
- **开发者文档**回答“代码怎么组织、怎么跑测试、为什么这样设计”。
- **ADR** 记录一次设计决定的背景、选项与后果，合并后只追加“状态”与“后续”，不改写决定本身。
- **归档**是开发过程中的计划、验收流水与实验记录，保留作为可追溯的依据，不再维护；新文档不要链接到归档里的过程细节，除非是作为证据引用。

## 5. 多语言

Avelis 的产品与文档支持三种语言：简体中文（`zh-CN`）、繁體中文（`zh-TW`）、English（`en`）。

### 哪些文档翻译

| 类别 | 语言 | 原因 |
|---|---|---|
| README、docs 索引、用户 / 运维 / 开发者文档、本规范 | 三语 | 使用、部署、参与开发都需要读懂 |
| `CONTRIBUTING`、`CODE_OF_CONDUCT`、`SECURITY` | 同一文件内三语分节 | 篇幅短，放在一起便于同步 |
| ADR、引擎核验记录、归档、`CHANGELOG` | 只有简体中文原文 | 是某一时刻的记录，翻译会造成多个“原文”；英文与繁中索引列出它们并注明“简体中文” |

### 文件命名

docs 下用**同名文件加语言后缀**：`deployment.md` 是简体中文原文，`deployment.en.md` 是英文，`deployment.zh-TW.md` 是繁體中文。不按语言分目录，原因是：

- 现有文档路径、代码注释与外部链接不变；
- 截图按界面语言分目录（见下方“截图”），文件名三种语言相同；
- 评审时原文与译文在同一目录，容易对照；
- 与常见文档站点工具的“后缀式”多语言约定一致，以后建站不用搬文件。

### 原文与同步

- **简体中文是原文**，英文与繁中是译文。README 也一样：GitHub 默认展示的 `README.md` 就是简体中文版。
- 改原文时在同一个 PR 里同步更新两份译文。来不及翻译时，在译文语言切换行下面加一行提示“本文落后于简体中文原文”，并开 issue 跟进。
- 繁中译文先用 [OpenCC](https://github.com/BYVoid/OpenCC) 的 `s2twp`（台湾用词）转换，再按下面的术语表逐项校对，不能只做字形转换。
- 英文译文按意思翻译，不逐字对应；命令、配置项、代码与路径保持原样。

### 语言切换行

每份三语文档在一级标题下面放一行语言切换，当前语言不加链接，顺序固定为 English、简体中文、繁體中文：

```markdown
# 部署指南

[English](deployment.en.md) | 简体中文 | [繁體中文](deployment.zh-TW.md)
```

### 链接

- 译文里的链接指向同一语言的版本：`deployment.en.md` 链接 `configuration.en.md`。
- 目标没有该语言版本时（ADR、引擎核验、归档），链接原文并在链接文字后注明：英文写 “(Simplified Chinese)”，繁中写“（簡體中文）”。

### 截图

- 截图按界面语言分目录：简体中文放 `docs/images/`，英文放 `docs/images/en/`，繁中放 `docs/images/zh-TW/`，文件名相同；各语言文档引用自己语言的截图。
- 替换截图时三种语言一起重拍（同一份演示数据、1440×900），同名覆盖。

### 术语对照

三种语言的译法以本表为准；新增术语先加到这里。“不写”一列只列简体中文里容易混用的写法。

| 简体中文 | 繁體中文 | English | 不写（简体中文） |
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

## 6. 写法

### 标题与结构

- 每个文件只有一个一级标题 `#`，即文档标题；正文从 `##` 开始，最多用到 `####`。
- 标题用名词短语，不加句号；编号只在步骤性文档里用（“1. 准备”）。
- 开头一两句说明这篇文档给谁看、解决什么问题。
- 一段只讲一件事；能用表格对照的不用长段落。

### 语言

- 句子短，用主动语态；面向用户的文档不用内部代号（M3、E1、P0、B0 等），需要时写成功能名。
- 中文与英文、数字之间加一个空格：“支持 StarRocks 3.5”。
- 中文用全角标点；代码、命令、路径、配置项用反引号。
- 不写“简单地”“只需”“显然”；英文同理不写 “simply”“just”“obviously”。
- 术语按第 5 节的对照表。

### 链接与图片

- 仓库内链接一律用相对路径：`[部署指南](deployment.md)`、`[ADR 0004](decisions/0004-engine-adapters.md)`。
- 不写本机绝对路径（`E:\...`、`C:\Users\...`）、内部会话链接或私有工作区链接。
- 截图放在 `docs/images/`，文件名用小写英文与连字符，描述画面内容：`analysis-dataset.png`。
- 截图必须来自真实运行的产品与演示数据，不放设计稿冒充功能；替换截图时同名覆盖，避免链接失效。
- 图片写替代文字，用文档本身的语言：`![血缘：选中表与检查器](images/lineage.png)`。

### 命令与代码块

- 代码块标注语言：`bash`、`powershell`、`toml`、`python`。
- 写进文档的命令必须实际运行过；只在特定平台成立的命令注明平台。
- 不在命令前加 `$`，不把输出混进命令块；需要展示输出时另起一个代码块。
- 示例口令、密钥一律用明显的占位符（`change-me`、`<your-secret>`），不写真实值。
- 译文里代码块的注释可以翻译，命令本身不改。

### 事实与状态

- 只写已经实现并验证过的功能；计划中的写进路线图，并标明“计划”。
- 写验证结论时写清范围：在什么环境、什么版本、跑了什么。
- 已知局限单独列出，不藏在正文里。

## 7. 提交与检查

- 文档改动与代码改动可以在同一个 PR，但 README 与 `docs/` 的重组单独提 PR。
- 提交前检查：相对链接与图片路径能打开；三份 README 与三语文档同步；没有本机路径与凭证。
- 提交说明与 PR 用简体中文，见 [CONTRIBUTING.md](../CONTRIBUTING.md)。
