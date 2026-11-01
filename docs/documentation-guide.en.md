# Documentation Guide

English | [简体中文](documentation-guide.md) | [繁體中文](documentation-guide.zh-TW.md)

This guide sets the structure, languages and style of the README and documentation in the Avelis repository. It follows the practice of open-source data and infrastructure projects such as Apache Superset, DataHub, OpenMetadata, Airbyte, dbt and Metabase, keeping only what suits Avelis today. The Simplified Chinese version is the original.

## 1. What the reference projects have in common

| Practice | Reference projects | How Avelis applies it |
|---|---|---|
| A row of badges under the title (license, version, build status, community) | Superset, DataHub, Airbyte, Metabase | Only badges that are real: license, CI, latest release; no badge for releases or community channels that do not exist yet |
| One-line positioning + one product screenshot | Metabase, DataHub | One sentence under the title says what it is and who it is for, followed by a real screenshot |
| Highlights as a short list or captioned screenshots | Superset (screenshots with bold captions), OpenMetadata | 4–6 highlights, one sentence each; screenshots in a two-column table with captions |
| A separate section for supported databases | Superset, Metabase | A “Supported databases” table showing how far each engine is supported |
| Quick start gives only the shortest runnable path and links to details | DataHub, Airbyte, Metabase | One Docker Compose block; external databases, reverse proxy, upgrades and so on link to the deployment guide |
| Docs grouped by reader: users / administrators / developers | Superset | `docs/README.md` is grouped into using and operating / developing / design decisions / archive |
| Separate files for contributing, code of conduct and security, linked from the README | dbt, Airbyte, Metabase | `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md` and `SECURITY.md` at the repository root |
| Security issues are not reported in public issues | Airbyte, Metabase | `SECURITY.md` describes the private reporting channel |
| License as the last section, one or two lines | DataHub, OpenMetadata | Same |

## 2. Files at the repository root

| File | Language | Contents |
|---|---|---|
| `README.md` | English | Project entry point, structured as in section 3; shown by default on GitHub |
| `README.zh-CN.md` | Simplified Chinese | Same structure and content as the English version; **the authoritative source** |
| `README.zh-TW.md` | Traditional Chinese | Same structure and content as the Simplified Chinese version |
| `CONTRIBUTING.md` | Three languages, one section each | Development setup, commit conventions, pull request process, testing requirements |
| `CODE_OF_CONDUCT.md` | Three languages, one section each | Adopts the Contributor Covenant 2.1 |
| `SECURITY.md` | Three languages, one section each | Supported versions, private reporting, response times |
| `CHANGELOG.md` | Simplified Chinese | Version changes in the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format |
| `LICENSE` | English | The Apache License 2.0 text, unchanged |
| `.github/` | Simplified Chinese + English | Issue templates (bilingual form labels), pull request template, CI |

All three READMEs must change together; see section 5.

## 3. README structure

In this order; omit sections that have no content:

1. **Title and badges**: `# Avelis`, a row of badges, then a line of language links.
2. **Positioning**: one bold sentence saying what it is, then one or two sentences on the problem it solves.
3. **Main screenshot**: the real screenshot that best represents the product.
4. **Highlights**: a short list, each item starting with a bold keyword.
5. **Screenshots**: a two-column table, one caption per image; more screenshots link to the product tour.
6. **Supported databases**: a table of the capabilities each engine supports.
7. **Quick start**: prerequisites + the shortest runnable commands + which address to open and which account to use.
8. **Architecture**: one Mermaid diagram + three to five points, with details linked to the architecture document.
9. **Configuration**: a table of required settings, linking to the full configuration reference.
10. **Development**: one block of commands each for backend and frontend, linking to the development guide.
11. **Documentation**: links grouped by reader.
12. **Roadmap**: only directions already decided, linked to issues or milestones; no dates.
13. **Contributing**: links to `CONTRIBUTING.md` and `CODE_OF_CONDUCT.md`.
14. **Security**: link to `SECURITY.md`.
15. **License**: one line, linking to `LICENSE`.

## 4. The docs directory

```
docs/
  README.md / README.en.md / README.zh-TW.md     documentation index (grouped by reader)
  product-tour.md (+ .en.md / .zh-TW.md)          users: product tour
  deployment.md (+ .en.md / .zh-TW.md)            operators: deployment guide
  configuration.md (+ .en.md / .zh-TW.md)         operators: configuration reference
  development.md (+ .en.md / .zh-TW.md)           developers: development guide
  architecture.md (+ .en.md / .zh-TW.md)          developers: architecture and governance
  documentation-guide.md (+ .en.md / .zh-TW.md)   developers: this guide
  engines/                 developers: verification records per database engine (Simplified Chinese only)
  decisions/               design decisions, ADRs (Simplified Chinese only)
  images/                  screenshots used by the README and docs
  archive/                 internal records: development plan, milestone acceptance, experiments (Simplified Chinese only, read-only)
```

- **User and operator docs** answer “how do I use, deploy and configure it”; no implementation details or development history.
- **Developer docs** answer “how is the code organized, how do I run the tests, why is it designed this way”.
- **ADRs** record the context, options and consequences of one design decision. After merging, only “status” and “follow-up” are appended; the decision itself is never rewritten.
- **The archive** holds plans, acceptance logs and experiment records from development. They are kept as traceable evidence and no longer maintained; new documents should not link to process details in the archive except as evidence.

## 5. Languages

The Avelis product and documentation support three languages: Simplified Chinese (`zh-CN`), Traditional Chinese (`zh-TW`) and English (`en`).

### What is translated

| Category | Languages | Why |
|---|---|---|
| READMEs, docs index, user / operator / developer docs, this guide | All three | Needed to use, deploy and contribute |
| `CONTRIBUTING`, `CODE_OF_CONDUCT`, `SECURITY` | Three sections in one file | Short; keeping them together makes them easier to keep in sync |
| ADRs, engine verification records, archive, `CHANGELOG` | Simplified Chinese original only | They are records of a moment in time, and translations would create several “originals”; the English and Traditional Chinese indexes list them and say they are in Simplified Chinese |

### File naming

In `docs/`, translations are **files with the same name plus a language suffix**: `deployment.md` is the Simplified Chinese original, `deployment.en.md` the English version and `deployment.zh-TW.md` the Traditional Chinese version. We do not use one directory per language because:

- existing document paths, code comments and external links stay valid;
- all three languages share `images/` with the same image paths;
- the original and translations sit side by side, which makes review easier;
- it matches the suffix convention of common documentation site generators, so building a site later needs no file moves.

### Originals and keeping in sync

- **Simplified Chinese is the original**; English and Traditional Chinese are translations. The README is the exception in presentation only: GitHub shows the English version by default, but the Simplified Chinese version remains the authoritative content.
- When the original changes, update both translations in the same pull request. If a translation cannot be updated in time, add the line “This page is behind the Simplified Chinese original.” under its language links and open an issue.
- Traditional Chinese translations start from [OpenCC](https://github.com/BYVoid/OpenCC) `s2twp` (Taiwan phrasing) and are then checked term by term against the table below; a character conversion alone is not enough.
- English translations follow the meaning rather than the words; commands, settings, code and paths stay as they are.

### Language links

Every three-language document has one line of language links under its title. The current language is not linked, and the order is always English, 简体中文, 繁體中文:

```markdown
# Deployment Guide

English | [简体中文](deployment.md) | [繁體中文](deployment.zh-TW.md)
```

### Links

- Links in a translation point to the same language: `deployment.en.md` links to `configuration.en.md`.
- When the target has no version in that language (ADRs, engine records, archive), link to the original and say so after the link text: “(Simplified Chinese)” in English, “（簡體中文）” in Traditional Chinese.

### Screenshots

- Until the product interface supports multiple languages, documents in all three languages use the current Simplified Chinese screenshots in `docs/images/`.
- Once it does, screenshots are retaken per language: English in `docs/images/en/`, Traditional Chinese in `docs/images/zh-TW/`, with the same file names as the Simplified Chinese ones, and translations switch to their own directory.

### Terminology

This table is the reference for translating terms in all three languages; add new terms here first. The “avoid” column lists terms that are easily confused in Simplified Chinese.

| 简体中文 | 繁體中文 | English | Avoid (简体中文) |
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

## 6. Style

### Headings and structure

- Each file has exactly one level-1 heading `#`, the document title; the body starts at `##` and goes no deeper than `####`.
- Headings are noun phrases without a period; numbering only in step-by-step documents (“1. Prepare”).
- The first one or two sentences say who the document is for and what it solves.
- One paragraph, one point; use a table instead of long paragraphs where things can be compared.

### Language

- Short sentences, active voice; user-facing documents do not use internal code names (M3, E1, P0, B0, …); use feature names instead.
- In Chinese, put a space between Chinese and Latin letters or numbers: “支持 StarRocks 3.5”.
- Chinese text uses full-width punctuation; code, commands, paths and settings go in backticks.
- Avoid “simply”, “just” and “obviously” (and their Chinese equivalents).
- Use the terminology table in section 5.

### Links and images

- Links within the repository are always relative: `[Deployment guide](deployment.en.md)`, `[ADR 0004](decisions/0004-engine-adapters.md)`.
- No local absolute paths (`E:\...`, `C:\Users\...`), internal session links or private workspace links.
- Screenshots go in `docs/images/`, named in lowercase English with hyphens, describing the content: `analysis-dataset.png`.
- Screenshots must come from the real running product with demo data, never design mock-ups presented as features; when replacing a screenshot, keep the file name so links do not break.
- Give images alt text in the document's own language: `![Lineage: selected table and inspector](images/lineage.png)`.

### Commands and code blocks

- Label code blocks with a language: `bash`, `powershell`, `toml`, `python`.
- Every command written in a document must have been run; note the platform for commands that only work on one.
- No `$` before commands and no output mixed into command blocks; show output in a separate block.
- Example passwords and keys are always obvious placeholders (`change-me`, `<your-secret>`), never real values.
- Comments in code blocks may be translated; the commands themselves are not changed.

### Facts and status

- Only describe features that are implemented and verified; planned work goes in the roadmap, marked as planned.
- When stating a verification result, say its scope: environment, version, what was run.
- List known limitations separately instead of hiding them in the text.

## 7. Submitting and checking

- Documentation and code changes may share a pull request, but reorganizations of the README and `docs/` get their own.
- Before submitting, check that relative links and image paths open, that the three READMEs and three-language docs are in sync, and that no local paths or credentials are included.
- Maintainers write commit messages and pull requests in Simplified Chinese; see [CONTRIBUTING.md](../CONTRIBUTING.md).
