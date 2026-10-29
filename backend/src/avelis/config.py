"""运行配置：只来自环境变量与仓库根目录的 .env（不进仓库）。

口令与密钥只在这里读入内存，不写日志、不回传给前端、不进模型上下文。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

REPO_ROOT = Path(__file__).resolve().parents[3]


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def load_env() -> dict[str, str]:
    """.env 提供默认值，进程环境变量优先。"""
    env = _read_env_file(Path(os.environ.get("AVELIS_ENV_FILE", REPO_ROOT / ".env")))
    env.update({k: v for k, v in os.environ.items() if k.startswith(("AVELIS_", "SR_", "SHOP_"))})
    return env


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    secret: str
    access_token_minutes: int = 30
    login_max_failures: int = 5
    login_lock_minutes: int = 15
    business_timezone: str = "Asia/Shanghai"
    allow_self_review: bool = False
    cors_origins: tuple[str, ...] = ("http://127.0.0.1:5173", "http://localhost:5173")
    query_timeout_seconds: int = 30
    query_mem_limit_bytes: int = 2 * 1024**3
    query_max_rows: int = 1000
    answer_cache_seconds: int = 300
    collect_interval_minutes: int = 15
    unassigned_asset_policy: str = "deny"
    ask_live_check: bool = True
    require_column_comments: bool = False
    worker_schedule_seconds: int = 60
    worker_stale_seconds: int = 30
    worker_running_timeout_seconds: int = 3600
    env: dict[str, str] = field(default_factory=dict, repr=False, compare=False)


def _database_url(env: dict[str, str]) -> str:
    """元数据库只支持 PostgreSQL（ADR 0003）。"""
    url = env.get("AVELIS_DATABASE_URL") or (
        f"postgresql+psycopg://avelis:{quote_plus(env.get('AVELIS_PG_PASSWORD', ''))}"
        f"@127.0.0.1:{env.get('AVELIS_PG_PORT', '15432')}/avelis")
    if not url.startswith("postgresql"):
        raise ValueError("元数据库只支持 PostgreSQL（AVELIS_DATABASE_URL 须以 postgresql 开头）")
    return url


def build_settings(env: dict[str, str] | None = None) -> Settings:
    env = load_env() if env is None else env
    redis_password = quote_plus(env.get("AVELIS_REDIS_PASSWORD", ""))
    redis_url = env.get("AVELIS_REDIS_URL") or (
        f"redis://:{redis_password}@127.0.0.1:{env.get('AVELIS_REDIS_PORT', '16379')}/0"
    )
    secret = env.get("AVELIS_SECRET", "")
    if len(secret) < 16:
        raise RuntimeError("缺少 AVELIS_SECRET（至少 16 个字符）。运行 uv run scripts/demo.py init-env 生成。")
    origins = env.get("AVELIS_CORS_ORIGINS")
    return Settings(
        database_url=_database_url(env),
        redis_url=redis_url,
        secret=secret,
        business_timezone=env.get("AVELIS_BUSINESS_TIMEZONE", "Asia/Shanghai"),
        allow_self_review=env.get("AVELIS_ALLOW_SELF_REVIEW", "false").lower() == "true",
        cors_origins=tuple(o.strip() for o in origins.split(",")) if origins else Settings.cors_origins,
        unassigned_asset_policy=env.get("AVELIS_UNASSIGNED_ASSET_POLICY", "deny").strip().lower(),
        login_max_failures=int(env.get("AVELIS_LOGIN_MAX_FAILURES", "5")),
        login_lock_minutes=int(env.get("AVELIS_LOGIN_LOCK_MINUTES", "15")),
        ask_live_check=env.get("AVELIS_ASK_LIVE_CHECK", "true").strip().lower() not in ("0", "false", "no"),
        require_column_comments=env.get("AVELIS_REQUIRE_COLUMN_COMMENTS", "false").strip().lower()
        in ("1", "true", "yes"),
        worker_schedule_seconds=int(env.get("AVELIS_WORKER_SCHEDULE_SECONDS", "60")),
        worker_stale_seconds=int(env.get("AVELIS_WORKER_STALE_SECONDS", "30")),
        worker_running_timeout_seconds=int(env.get("AVELIS_WORKER_RUNNING_TIMEOUT_SECONDS", "3600")),
        env=env,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return build_settings()
