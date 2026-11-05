# syntax=docker/dockerfile:1
# Avelis 应用镜像：前端构建 → 后端依赖安装 → 运行镜像（非 root）。
# 同一镜像既跑 API（默认命令，同时托管前端），也跑 worker（命令改为 python -m avelis.jobs）。
# 所有依赖（前端产物、Python 依赖、字体、tiktoken 编码表）都在构建期装进镜像；运行镜像不含 uv / pnpm，启动时不联网下载。
#   docker build -t avelis:1.0.6 .
# 部署编排见 deploy/docker/compose.yaml 与 docs/deployment.md。

ARG NODE_IMAGE=node:24.13.0-slim
ARG PYTHON_IMAGE=python:3.12.14-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.16

FROM ${UV_IMAGE} AS uv

# ---------- 前端：pnpm 构建静态资源 ----------
FROM ${NODE_IMAGE} AS frontend
ARG PNPM_VERSION=11.8.0
ENV PNPM_HOME=/pnpm PATH=/pnpm:$PATH CI=true
RUN npm install -g pnpm@${PNPM_VERSION} --no-fund --no-audit
WORKDIR /src/frontend
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN --mount=type=cache,id=avelis-pnpm,target=/pnpm/store pnpm install --frozen-lockfile --store-dir /pnpm/store
COPY frontend/ ./
RUN pnpm build

# ---------- 后端：uv 按锁文件安装依赖 ----------
FROM ${PYTHON_IMAGE} AS backend
COPY --from=uv /uv /usr/local/bin/uv
# git 只在构建阶段使用：sqlglot 取自 fork 的固定提交（backend/pyproject.toml 的 tool.uv.sources），运行镜像不含 git
RUN apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_HTTP_TIMEOUT=300 UV_HTTP_RETRIES=5
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=cache,id=avelis-uv,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
# 运行期不联网下载任何东西：tiktoken（langchain-openai 的依赖）首次使用时会下载编码表，构建时预置进镜像
ENV TIKTOKEN_CACHE_DIR=/app/.cache/tiktoken
RUN /app/.venv/bin/python -c "import tiktoken; [tiktoken.get_encoding(n) for n in ('cl100k_base', 'o200k_base')]"
COPY backend/alembic.ini ./
COPY backend/src ./src
# 项目以可编辑方式安装：源码留在 /app/backend/src，迁移脚本与 alembic.ini 按仓库布局（/app/backend）定位
RUN --mount=type=cache,id=avelis-uv,target=/root/.cache/uv uv sync --frozen --no-dev

# ---------- 运行镜像 ----------
FROM ${PYTHON_IMAGE} AS runtime
# 中文字体：分析报告导出 PDF 时嵌入（也可用 AVELIS_PDF_FONT 指定字体文件）
RUN apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends fonts-wqy-microhei \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 avelis \
    && useradd --system --uid 10001 --gid avelis --home-dir /app --shell /usr/sbin/nologin avelis \
    && install -d -o avelis -g avelis -m 0755 /run/avelis-secrets /etc/avelis
WORKDIR /app
COPY --from=backend --chown=avelis:avelis /app /app
COPY --from=frontend --chown=avelis:avelis /src/frontend/dist /app/frontend/dist

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TIKTOKEN_CACHE_DIR=/app/.cache/tiktoken \
    AVELIS_HOST=0.0.0.0 \
    AVELIS_PORT=8000 \
    AVELIS_FRONTEND_DIR=/app/frontend/dist

USER avelis
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=5 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('AVELIS_PORT', '8000'), timeout=4)"]
CMD ["python", "-m", "avelis.server"]
