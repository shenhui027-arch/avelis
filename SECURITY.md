# Security Policy

[English](#english) | [简体中文](#简体中文) | [繁體中文](#繁體中文)

## English

### Supported versions

This policy covers Avelis v1.0.0. Security fixes are made on the `main` branch.

| Version | Supported |
|---|---|
| v1.0.0 | Yes |
| `main` | Yes |
| Anything else | No |

### Reporting a vulnerability

Please **do not** report security vulnerabilities through public GitHub issues, discussions or pull requests.

Report them privately by email to [shenhui027@gmail.com](mailto:shenhui027@gmail.com), or through GitHub: open the repository's **Security** tab and choose **Report a vulnerability** ([direct link](https://github.com/shenhui027-arch/avelis/security/advisories/new)). Include:

- the affected component (API endpoint, page, worker, deployment file) and version or commit;
- steps to reproduce, with a minimal configuration;
- the impact you observed or expect.

You should receive an acknowledgement within 5 working days. We will keep you informed while we investigate and will credit you in the advisory unless you prefer otherwise.

### Scope

In scope: the Avelis backend, frontend, worker, Docker image and the files under `deploy/docker/`.

Out of scope: the local demo environment under `deploy/demo/` (it is for development only, binds to `127.0.0.1` and uses generated passwords), third-party model services, and the databases Avelis connects to.

### Hardening notes

- Change every password and the `[security] secret` in `avelis.toml` before the first start; never commit `avelis.toml` or `.env`.
- Give Avelis a read-only account for queries and a metadata-only account for collection.
- By default Avelis sends query results and (redacted) code snippets to the configured model provider, because the analysis agent needs them. To stop this, turn off **发送查询结果** (send query results) and **发送代码片段** (send code snippets) under **设置 → 模型配置** (Settings → Models). If your data must not leave your network, connect a locally hosted model through an OpenAI-compatible endpoint instead.
- Put Avelis behind HTTPS in production; see the [deployment guide](docs/deployment.en.md).

## 简体中文

### 支持的版本

本安全政策适用于 Avelis v1.0.0，安全修复在 `main` 分支进行。

### 报告漏洞

请**不要**通过公开的 issue、讨论或 PR 报告安全漏洞。

请发邮件至 [shenhui027@gmail.com](mailto:shenhui027@gmail.com)，或通过 GitHub 私下报告：打开仓库的 **Security** 页签，选择 **Report a vulnerability**（[直达链接](https://github.com/shenhui027-arch/avelis/security/advisories/new)）。请写明：

- 受影响的部分（接口、页面、worker、部署文件）与版本或提交；
- 复现步骤与最小配置；
- 已观察到或预期的影响。

我们会在 5 个工作日内确认收到，调查过程中保持沟通，并在安全公告中致谢（除非你不希望署名）。

### 范围

范围内：Avelis 后端、前端、worker、Docker 镜像与 `deploy/docker/` 下的部署文件。

范围外：`deploy/demo/` 下的本地演示环境（只用于开发，只绑定 `127.0.0.1`，口令随机生成）、第三方模型服务、Avelis 连接的数据库本身。

### 加固建议

- 首次启动前修改 `avelis.toml` 中的全部口令与 `[security] secret`；不要提交 `avelis.toml` 或 `.env`。
- 给 Avelis 分配只读查询账号与只读元数据的采集账号。
- Avelis 默认把查询结果与（已脱敏的）代码片段发送给配置的模型服务，分析智能体需要它们。要停止发送，在 **设置 → 模型配置** 关闭“发送查询结果”与“发送代码片段”。数据不能离开内网时，请通过 OpenAI 兼容接口接入本地部署的模型。
- 生产环境放在 HTTPS 之后，见[部署指南](docs/deployment.md)。

## 繁體中文

### 支援的版本

本安全政策適用於 Avelis v1.0.0，安全修復在 `main` 分支進行。

### 報告漏洞

請**不要**透過公開的 issue、討論或 PR 報告安全漏洞。

請寄信至 [shenhui027@gmail.com](mailto:shenhui027@gmail.com)，或透過 GitHub 私下報告：開啟儲存庫的 **Security** 頁籤，選擇 **Report a vulnerability**（[直達連結](https://github.com/shenhui027-arch/avelis/security/advisories/new)）。請寫明：

- 受影響的部分（介面、頁面、worker、部署檔案）與版本或提交；
- 復現步驟與最小設定；
- 已觀察到或預期的影響。

我們會在 5 個工作日內確認收到，調查過程中保持溝通，並在安全公告中致謝（除非你不希望署名）。

### 範圍

範圍內：Avelis 後端、前端、worker、Docker 映像與 `deploy/docker/` 下的部署檔案。

範圍外：`deploy/demo/` 下的本地示範環境（只用於開發，只繫結 `127.0.0.1`，密碼隨機生成）、第三方模型服務、Avelis 連線的資料庫本身。

### 加固建議

- 首次啟動前修改 `avelis.toml` 中的全部密碼與 `[security] secret`；不要提交 `avelis.toml` 或 `.env`。
- 給 Avelis 分配唯讀查詢帳號與唯讀中繼資料的採集帳號。
- Avelis 預設把查詢結果與（已脫敏的）程式碼片段傳送給設定的模型服務，分析代理需要它們。要停止傳送，在 **設定 → 模型設定** 關閉「傳送查詢結果」與「傳送程式碼片段」。資料不能離開內網時，請透過 OpenAI 相容介面接入本地部署的模型。
- 生產環境放在 HTTPS 之後，見[部署指南](docs/deployment.zh-TW.md)。
