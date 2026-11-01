# Security Policy

[English](#english) | [简体中文](#简体中文)

## English

### Supported versions

Avelis has not published a stable release yet. Security fixes are made on the `main` branch only.

| Version | Supported |
|---|---|
| `main` | Yes |
| Anything else | No |

### Reporting a vulnerability

Please **do not** report security vulnerabilities through public GitHub issues, discussions or pull requests.

Report them privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability** ([direct link](https://github.com/shenhui027-arch/avelis/security/advisories/new)). Include:

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
- Put Avelis behind HTTPS in production; see the [deployment guide](docs/deployment.md).

## 简体中文

### 支持的版本

Avelis 还没有发布稳定版本，安全修复只在 `main` 分支进行。

### 报告漏洞

请**不要**通过公开的 issue、讨论或 PR 报告安全漏洞。

请通过 GitHub 私下报告：打开仓库的 **Security** 页签，选择 **Report a vulnerability**（[直达链接](https://github.com/shenhui027-arch/avelis/security/advisories/new)）。请写明：

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
