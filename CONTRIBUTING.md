# Contributing to Avelis

[English](#english) | [简体中文](#简体中文)

## English

Thanks for your interest in Avelis. Bug reports, documentation fixes, new database engine adapters and feature proposals are all welcome. Issues and pull requests may be written in English or Chinese; maintainers usually reply in Simplified Chinese.

### Before you start

- Search [existing issues](https://github.com/shenhui027-arch/avelis/issues) first. For anything larger than a bug fix, open an issue to discuss the design before writing code.
- Read the [architecture overview](docs/architecture.md) and the [architecture decision records](docs/decisions/). Changes that contradict an accepted ADR need a new ADR.
- Product code must stay business-neutral ([ADR 0010](docs/decisions/0010-business-neutral-product.md)): no table names, metrics or industry rules from the demo datasets in `backend/src` or `frontend/src`.
- Report security issues privately as described in [SECURITY.md](SECURITY.md), not in a public issue.

### Development setup

Prerequisites: [uv](https://docs.astral.sh/uv/) (installs Python 3.12), Node.js 24 with pnpm 11, and Docker with Compose v2 for the demo databases. See the [development guide](docs/development.md) for the full setup.

```bash
# Backend
cd backend
uv sync
uvx ruff@0.17.3 check .
AVELIS_SECRET=offline-unit-test-secret-0123456789 AVELIS_ENV_FILE=/nonexistent uv run --frozen python -m pytest tests/unit -q -p no:cacheprovider

# Frontend
cd frontend
pnpm install
pnpm build
```

Unit tests run offline and never call a model or a database. Acceptance tests (`pytest -m acceptance`) reset the local demo environment; run them only on your own machine.

### Pull requests

1. Fork the repository and create a branch from `main`.
2. Keep each pull request focused on one change. Separate refactoring from behaviour changes.
3. Add or update tests. Every bug fix needs a test that fails without the fix.
4. Make sure `ruff check`, the unit tests and `pnpm build` pass. CI runs the same checks on every pull request.
5. Update the docs when behaviour, configuration or commands change; follow the [documentation guide](docs/documentation-guide.md).
6. Fill in the pull request template, including what you verified and what you did not.

Fixes must be general. Do not special-case an evaluation question, a demo table or a single customer schema to make a test pass.

### Adding a database engine

Each database is an adapter in `backend/src/avelis/engines/`. Follow [ADR 0004](docs/decisions/0004-engine-adapters.md): implement the connector, dialect and capability declaration, and add unit tests. Only engines supported by [SQLGlot](https://github.com/tobymao/sqlglot) can be added.

### License

By contributing, you agree that your contributions are licensed under the [Apache License 2.0](LICENSE).

## 简体中文

感谢关注 Avelis。欢迎报告缺陷、改进文档、新增数据库引擎适配器和提出功能建议。Issue 与 PR 可以用中文或英文；维护者一般用简体中文回复。

### 开始之前

- 先搜索[已有 issue](https://github.com/shenhui027-arch/avelis/issues)。比修缺陷更大的改动，请先开 issue 讨论设计再写代码。
- 阅读[架构与治理说明](docs/architecture.md)与[架构决策记录](docs/decisions/)。与已接受的 ADR 相悖的改动需要新的 ADR。
- 产品代码保持业务中立（[ADR 0010](docs/decisions/0010-business-neutral-product.md)）：`backend/src` 与 `frontend/src` 里不写演示数据集中的表名、指标或行业规则。
- 安全问题按 [SECURITY.md](SECURITY.md) 私下报告，不要开公开 issue。

### 开发环境

前置条件：[uv](https://docs.astral.sh/uv/)（自动安装 Python 3.12）、Node.js 24 与 pnpm 11、Docker（Compose v2，用于演示数据库）。完整步骤见[开发指南](docs/development.md)。

```bash
# 后端
cd backend
uv sync
uvx ruff@0.17.3 check .
AVELIS_SECRET=offline-unit-test-secret-0123456789 AVELIS_ENV_FILE=/nonexistent uv run --frozen python -m pytest tests/unit -q -p no:cacheprovider

# 前端
cd frontend
pnpm install
pnpm build
```

单元测试离线运行，不调用模型也不连数据库。验收测试（`pytest -m acceptance`）会重置本机演示环境，只在自己的机器上运行。

### 提交 PR

1. Fork 仓库，从 `main` 建分支。
2. 一个 PR 只做一件事；重构与行为变化分开提交。
3. 补充或更新测试。修复缺陷要有一个“不修就失败”的测试。
4. 确认 `ruff check`、单元测试与 `pnpm build` 通过；CI 会在每个 PR 上运行同样的检查。
5. 行为、配置或命令变化时同步更新文档，写法见[文档规范](docs/documentation-guide.md)。
6. 按 PR 模板填写，写清验证了什么、没验证什么。

修复必须通用：不为了让测试通过而针对某道评测题、某张演示表或某个客户的表结构写特殊处理。

### 提交说明

维护者的提交说明与 PR 标题、正文用简体中文：标题一句话写清改了什么，正文写原因与影响。外部贡献者用英文也可以。

### 新增数据库引擎

每种数据库是 `backend/src/avelis/engines/` 中的一个适配器。按 [ADR 0004](docs/decisions/0004-engine-adapters.md) 实现连接器、方言与能力声明，并补单元测试。只接入 [SQLGlot](https://github.com/tobymao/sqlglot) 支持的引擎。

### 许可

提交贡献即表示你同意贡献内容按 [Apache License 2.0](LICENSE) 授权。
