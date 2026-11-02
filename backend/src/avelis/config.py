"""运行配置：环境变量 > .env > TOML > 内置默认值。

口令与密钥只在这里读入内存，不写日志、不回传给前端、不进模型上下文。
"""

from __future__ import annotations

import argparse
import math
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, quote_plus, unquote, urlsplit

from avelis.configfile import (
    CONFIG_KEYS,
    SENSITIVE_KEYS,
    ConfigError,
    ConfigValues,
    flatten,
    merge_layers,
    read_config,
    report_config_error,
    resolve_files,
)
from avelis.i18n import gettext

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


def env_file() -> Path:
    """配置文件位置：AVELIS_ENV_FILE（相对路径按仓库根目录解析，与 scripts/demo.py 一致），默认仓库根目录的 .env。"""
    path = Path(os.environ.get("AVELIS_ENV_FILE") or ".env")
    return path if path.is_absolute() else REPO_ROOT / path


def load_env() -> dict[str, str]:
    """逐层解析敏感文件引用后合并；配置文件位置只取进程环境。"""
    config = read_config(os.environ)
    dotenv = ConfigValues(source=".env", config_path=config.config_path)
    try:
        dotenv.update(_read_env_file(env_file()))
        dotenv.pop("AVELIS_CONFIG", None)
        dotenv.sources.update(dict.fromkeys(dotenv, ".env"))
    except (OSError, ValueError):
        dotenv.problem("AVELIS_ENV_FILE", f"无法读取 .env 文件 {env_file()}", source="环境变量")
    process = ConfigValues(
        {k: v for k, v in os.environ.items() if k.startswith(("AVELIS_", "SR_", "SHOP_"))},
        config_path=config.config_path,
    )
    return merge_layers(resolve_files(config), resolve_files(dotenv), resolve_files(process))


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    secret: str
    access_token_minutes: int = 120  # 单个登录凭证有效期（闲置超时），单位分钟
    session_max_hours: int = 168  # 一次登录最长可续期时长，单位小时；到期后必须重新登录
    login_max_failures: int = 5
    login_lock_minutes: int = 15
    business_timezone: str = "Asia/Shanghai"
    number_style: str = "cn"
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
    material_change_ratio: float = 0.01  # 变化显著性阈值（相对对比期，ADR 0009 第 7 点）：口径版本未单独配置时使用
    analysis_query_limit: int = 20  # 一次分析里查询类工具共用的额度（ADR 0009 第 7 点第 7 条）
    analysis_retrieval: bool = False  # 元数据图证据发现（P1 R）
    analysis_investigation: bool = False  # 证据解释（P1 H），本阶段只定义开关
    analysis_uniform_tolerance: float = 0.5  # 成员变化率相对总体变化率的均匀容差
    analysis_spike_share: float = 0.5  # 最大单日偏差占总偏差的时间集中度阈值
    worker_schedule_seconds: int = 60
    worker_stale_seconds: int = 30
    worker_running_timeout_seconds: int = 3600
    history_retention_days: int = 1095
    history_purge_interval_hours: int = 24
    history_export_max_rows: int = 50000
    ddl_audit_slack_seconds: int = 5
    host: str = "127.0.0.1"
    port: int = 8000
    workers: int = 1
    public_url: str = ""
    mcp_enabled: bool = False
    mcp_access_token_seconds: int = 900
    mcp_refresh_token_seconds: int = 30 * 24 * 3600
    mcp_cimd_cache_ttl_seconds: int = 3600
    mcp_cimd_allowed_urls: tuple[str, ...] = ()
    forwarded_allow_ips: str = "127.0.0.1"
    log_level: str = "info"
    frontend_dir: Path | None = None
    auto_migrate: bool = False
    auto_init_admin: bool = False
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_recycle_seconds: int = 1800
    import_max_bytes: int = 20 * 1024**2
    import_max_batch_bytes: int = 100 * 1024**2
    ask_max_tool_rounds: int = 8
    reply_language: str = "auto"
    analysis_tool_calls: int = 30
    analysis_seconds: int = 300
    analysis_explore_limit: int = 8
    analysis_model_requests: int = 16
    analysis_input_tokens: int | None = 552321
    analysis_output_tokens: int | None = 30716
    model_timeout_seconds: int = 60
    env: dict[str, str] = field(default_factory=dict, repr=False, compare=False)

    @property
    def mcp_resource_url(self) -> str:
        return self.public_url + "/mcp"


def _database_url(env: dict[str, str]) -> str:
    """元数据库只支持 PostgreSQL（ADR 0003）。"""
    url = env.get("AVELIS_DATABASE_URL")
    if not url and env.get("AVELIS_DB_HOST"):
        user = quote(env.get("AVELIS_DB_USER", "avelis"), safe="")
        password = quote(env.get("AVELIS_DB_PASSWORD", ""), safe="")
        host = _url_host(env["AVELIS_DB_HOST"])
        name = quote(env.get("AVELIS_DB_NAME", "avelis"), safe="")
        url = f"postgresql+psycopg://{user}:{password}@{host}:{env.get('AVELIS_DB_PORT', '5432')}/{name}"
        sslmode = env.get("AVELIS_DB_SSLMODE", "").strip().lower()
        if sslmode:
            url += f"?sslmode={sslmode}"
    if not url:
        url = (
            f"postgresql+psycopg://avelis:{quote_plus(env.get('AVELIS_PG_PASSWORD', ''))}"
            f"@127.0.0.1:{env.get('AVELIS_PG_PORT', '15432')}/avelis"
        )
    if not url.startswith("postgresql"):
        raise ValueError("元数据库只支持 PostgreSQL（AVELIS_DATABASE_URL 须以 postgresql 开头）")
    return url


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _redis_url(env: dict[str, str]) -> str:
    if env.get("AVELIS_REDIS_URL"):
        return env["AVELIS_REDIS_URL"]
    if env.get("AVELIS_REDIS_HOST"):
        scheme = "rediss" if _boolean(env, "AVELIS_REDIS_SSL", False) else "redis"
        password = quote(env.get("AVELIS_REDIS_PASSWORD", ""), safe="")
        host = _url_host(env["AVELIS_REDIS_HOST"])
        return f"{scheme}://:{password}@{host}:{env.get('AVELIS_REDIS_PORT', '6379')}/{env.get('AVELIS_REDIS_DB', '0')}"
    password = quote_plus(env.get("AVELIS_REDIS_PASSWORD", ""))
    return f"redis://:{password}@127.0.0.1:{env.get('AVELIS_REDIS_PORT', '16379')}/0"


def _positive_int(env: dict[str, str], key: str, default: int) -> int:
    try:
        value = int(env.get(key, str(default)))
    except ValueError:
        raise ValueError(f"{key} 必须为正整数") from None
    if value <= 0:
        raise ValueError(f"{key} 必须为正整数")
    return value


def _nonnegative_int(env: dict[str, str], key: str, default: int) -> int:
    try:
        value = int(env.get(key, str(default)))
    except ValueError:
        raise ValueError(f"{key} 必须为非负整数") from None
    if value < 0:
        raise ValueError(f"{key} 必须为非负整数")
    return value


def _unit_interval(env: dict[str, str], key: str, default: float) -> float:
    try:
        value = float(env.get(key, str(default)))
    except ValueError:
        raise ValueError(gettext("{key} 必须为 (0, 1] 范围内的有限数值", key=key)) from None
    if not math.isfinite(value) or not 0 < value <= 1:
        raise ValueError(gettext("{key} 必须为 (0, 1] 范围内的有限数值", key=key))
    return value


def _bounded_int(env: dict[str, str], key: str, default: int, maximum: int) -> int:
    value = _positive_int(env, key, default)
    if value > maximum:
        raise ValueError(f"{key} 必须在 1–{maximum} 之间")
    return value


def _boolean(env: dict[str, str], key: str, default: bool) -> bool:
    value = env.get(key, str(default)).strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{key} 必须为布尔值（1/true/yes/on 或 0/false/no/off）")


def _nonempty_string(env: dict[str, str], key: str, default: str) -> str:
    value = env.get(key, default).strip()
    if not value:
        raise ValueError(f"{key} 不能为空")
    return value


def _enum(env: dict[str, str], key: str, default: str, choices: tuple[str, ...]) -> str:
    value = env.get(key, default).strip().lower()
    if value not in choices:
        raise ValueError(f"{key} 必须为以下值之一：{', '.join(choices)}")
    return value


def _reply_language(env: dict[str, str]) -> str:
    value = _enum(env, "AVELIS_REPLY_LANGUAGE", Settings.reply_language, ("auto", "zh-cn", "zh-tw", "en"))
    return {"zh-cn": "zh-CN", "zh-tw": "zh-TW"}.get(value, value)


def _frontend_directory(env: dict[str, str]) -> Path | None:
    value = env.get("AVELIS_FRONTEND_DIR", "").strip()
    if not value:
        return None
    try:
        path = Path(value)
        return (path if path.is_absolute() else REPO_ROOT / path).resolve()
    except (ValueError, OSError):
        raise ValueError("AVELIS_FRONTEND_DIR 必须为有效路径") from None


def _build_settings(env: dict[str, str]) -> Settings:
    redis_url = _redis_url(env)
    secret = env.get("AVELIS_SECRET", "")
    origins = env.get("AVELIS_CORS_ORIGINS")
    mcp_enabled = _boolean(env, "AVELIS_MCP_ENABLED", Settings.mcp_enabled)
    public_url = env.get("AVELIS_PUBLIC_URL", "").strip().rstrip("/")
    cors_origins = tuple(o.strip() for o in origins.split(",")) if origins else Settings.cors_origins
    parsed_public = urlsplit(public_url)
    public_origin = f"{parsed_public.scheme}://{parsed_public.netloc}" if mcp_enabled and public_url else public_url
    if public_origin and public_origin not in cors_origins:
        cors_origins += (public_origin,)
    return Settings(
        database_url=_database_url(env),
        redis_url=redis_url,
        secret=secret,
        access_token_minutes=_positive_int(env, "AVELIS_ACCESS_TOKEN_MINUTES", Settings.access_token_minutes),
        session_max_hours=_positive_int(env, "AVELIS_SESSION_MAX_HOURS", Settings.session_max_hours),
        business_timezone=env.get("AVELIS_BUSINESS_TIMEZONE", "Asia/Shanghai"),
        number_style=_enum(env, "AVELIS_NUMBER_STYLE", Settings.number_style, ("cn", "intl")),
        allow_self_review=env.get("AVELIS_ALLOW_SELF_REVIEW", "false").lower() == "true",
        cors_origins=cors_origins,
        query_timeout_seconds=_positive_int(env, "AVELIS_QUERY_TIMEOUT_SECONDS", Settings.query_timeout_seconds),
        query_mem_limit_bytes=_positive_int(env, "AVELIS_QUERY_MEM_LIMIT_BYTES", Settings.query_mem_limit_bytes),
        query_max_rows=_positive_int(env, "AVELIS_QUERY_MAX_ROWS", Settings.query_max_rows),
        answer_cache_seconds=_positive_int(env, "AVELIS_ANSWER_CACHE_SECONDS", Settings.answer_cache_seconds),
        collect_interval_minutes=_bounded_int(
            env, "AVELIS_COLLECT_INTERVAL_MINUTES", Settings.collect_interval_minutes, 1440
        ),
        unassigned_asset_policy=env.get("AVELIS_UNASSIGNED_ASSET_POLICY", "deny").strip().lower(),
        login_max_failures=int(env.get("AVELIS_LOGIN_MAX_FAILURES", "5")),
        login_lock_minutes=int(env.get("AVELIS_LOGIN_LOCK_MINUTES", "15")),
        ask_live_check=env.get("AVELIS_ASK_LIVE_CHECK", "true").strip().lower() not in ("0", "false", "no"),
        require_column_comments=env.get("AVELIS_REQUIRE_COLUMN_COMMENTS", "false").strip().lower()
        in ("1", "true", "yes"),
        material_change_ratio=float(env.get("AVELIS_MATERIAL_CHANGE_RATIO", "0.01")),
        analysis_query_limit=int(env.get("AVELIS_ANALYSIS_QUERY_LIMIT", "20")),
        analysis_retrieval=env.get("AVELIS_ANALYSIS_RETRIEVAL", "false").strip().lower() in ("1", "true", "yes"),
        analysis_investigation=env.get("AVELIS_ANALYSIS_INVESTIGATION", "false").strip().lower()
        in ("1", "true", "yes"),
        analysis_uniform_tolerance=_unit_interval(
            env, "AVELIS_ANALYSIS_UNIFORM_TOLERANCE", Settings.analysis_uniform_tolerance
        ),
        analysis_spike_share=_unit_interval(env, "AVELIS_ANALYSIS_SPIKE_SHARE", Settings.analysis_spike_share),
        worker_schedule_seconds=int(env.get("AVELIS_WORKER_SCHEDULE_SECONDS", "60")),
        worker_stale_seconds=int(env.get("AVELIS_WORKER_STALE_SECONDS", "30")),
        worker_running_timeout_seconds=int(env.get("AVELIS_WORKER_RUNNING_TIMEOUT_SECONDS", "3600")),
        history_retention_days=int(env.get("AVELIS_HISTORY_RETENTION_DAYS", "1095")),
        history_purge_interval_hours=int(env.get("AVELIS_HISTORY_PURGE_INTERVAL_HOURS", "24")),
        history_export_max_rows=int(env.get("AVELIS_HISTORY_EXPORT_MAX_ROWS", "50000")),
        ddl_audit_slack_seconds=int(env.get("AVELIS_DDL_AUDIT_SLACK_SECONDS", "5")),
        host=_nonempty_string(env, "AVELIS_HOST", Settings.host),
        port=_bounded_int(env, "AVELIS_PORT", Settings.port, 65535),
        workers=_positive_int(env, "AVELIS_WORKERS", Settings.workers),
        public_url=public_url,
        mcp_enabled=mcp_enabled,
        mcp_access_token_seconds=_positive_int(
            env, "AVELIS_MCP_ACCESS_TOKEN_SECONDS", Settings.mcp_access_token_seconds
        ),
        mcp_refresh_token_seconds=_positive_int(
            env, "AVELIS_MCP_REFRESH_TOKEN_SECONDS", Settings.mcp_refresh_token_seconds
        ),
        mcp_cimd_cache_ttl_seconds=_positive_int(
            env, "AVELIS_MCP_CIMD_CACHE_TTL_SECONDS", Settings.mcp_cimd_cache_ttl_seconds
        ),
        mcp_cimd_allowed_urls=tuple(
            u.strip() for u in env.get("AVELIS_MCP_CIMD_ALLOWED_URLS", "").split(",") if u.strip()
        ),
        forwarded_allow_ips=_nonempty_string(env, "AVELIS_FORWARDED_ALLOW_IPS", Settings.forwarded_allow_ips),
        log_level=_enum(
            env, "AVELIS_LOG_LEVEL", Settings.log_level, ("critical", "error", "warning", "info", "debug", "trace")
        ),
        frontend_dir=_frontend_directory(env),
        auto_migrate=_boolean(env, "AVELIS_AUTO_MIGRATE", Settings.auto_migrate),
        auto_init_admin=_boolean(env, "AVELIS_AUTO_INIT_ADMIN", Settings.auto_init_admin),
        db_pool_size=_nonnegative_int(env, "AVELIS_DB_POOL_SIZE", Settings.db_pool_size),
        db_max_overflow=_nonnegative_int(env, "AVELIS_DB_MAX_OVERFLOW", Settings.db_max_overflow),
        db_pool_recycle_seconds=_nonnegative_int(
            env, "AVELIS_DB_POOL_RECYCLE_SECONDS", Settings.db_pool_recycle_seconds
        ),
        import_max_bytes=_positive_int(env, "AVELIS_IMPORT_MAX_BYTES", Settings.import_max_bytes),
        import_max_batch_bytes=_positive_int(env, "AVELIS_IMPORT_MAX_BATCH_BYTES", Settings.import_max_batch_bytes),
        ask_max_tool_rounds=_positive_int(env, "AVELIS_ASK_MAX_TOOL_ROUNDS", Settings.ask_max_tool_rounds),
        reply_language=_reply_language(env),
        analysis_tool_calls=_positive_int(env, "AVELIS_ANALYSIS_TOOL_CALLS", Settings.analysis_tool_calls),
        analysis_seconds=_positive_int(env, "AVELIS_ANALYSIS_SECONDS", Settings.analysis_seconds),
        analysis_explore_limit=_positive_int(env, "AVELIS_ANALYSIS_EXPLORE_LIMIT", Settings.analysis_explore_limit),
        analysis_model_requests=_positive_int(env, "AVELIS_ANALYSIS_MODEL_REQUESTS", Settings.analysis_model_requests),
        analysis_input_tokens=_nonnegative_int(env, "AVELIS_ANALYSIS_INPUT_TOKENS", Settings.analysis_input_tokens)
        or None,
        analysis_output_tokens=_nonnegative_int(env, "AVELIS_ANALYSIS_OUTPUT_TOKENS", Settings.analysis_output_tokens)
        or None,
        model_timeout_seconds=_positive_int(env, "AVELIS_MODEL_TIMEOUT_SECONDS", Settings.model_timeout_seconds),
        env=env,
    )


def build_settings(env: dict[str, str] | None = None) -> Settings:
    """先收集全部问题，再复用既有解析；显式字典仍是独立的单层输入。"""
    if env is None:
        values = load_env()
    elif isinstance(env, ConfigValues):
        values = merge_layers(env)
    else:
        values = merge_layers(resolve_files(ConfigValues(env)))
    if not isinstance(values, ConfigValues):
        values = ConfigValues(values)
    positive = {
        "AVELIS_ACCESS_TOKEN_MINUTES",
        "AVELIS_SESSION_MAX_HOURS",
        "AVELIS_QUERY_TIMEOUT_SECONDS",
        "AVELIS_QUERY_MEM_LIMIT_BYTES",
        "AVELIS_QUERY_MAX_ROWS",
        "AVELIS_ANSWER_CACHE_SECONDS",
        "AVELIS_WORKERS",
        "AVELIS_IMPORT_MAX_BYTES",
        "AVELIS_IMPORT_MAX_BATCH_BYTES",
        "AVELIS_ASK_MAX_TOOL_ROUNDS",
        "AVELIS_ANALYSIS_TOOL_CALLS",
        "AVELIS_ANALYSIS_SECONDS",
        "AVELIS_ANALYSIS_EXPLORE_LIMIT",
        "AVELIS_ANALYSIS_MODEL_REQUESTS",
        "AVELIS_MODEL_TIMEOUT_SECONDS",
        "AVELIS_MCP_ACCESS_TOKEN_SECONDS",
        "AVELIS_MCP_REFRESH_TOKEN_SECONDS",
        "AVELIS_MCP_CIMD_CACHE_TTL_SECONDS",
    }
    nonnegative = {
        "AVELIS_DB_POOL_SIZE",
        "AVELIS_DB_MAX_OVERFLOW",
        "AVELIS_DB_POOL_RECYCLE_SECONDS",
        "AVELIS_ANALYSIS_INPUT_TOKENS",
        "AVELIS_ANALYSIS_OUTPUT_TOKENS",
        "AVELIS_REDIS_DB",
    }
    bounded = {"AVELIS_PORT": 65535, "AVELIS_DB_PORT": 65535, "AVELIS_COLLECT_INTERVAL_MINUTES": 1440}
    for spec in CONFIG_KEYS.values():
        key, default = spec.env, spec.default
        try:
            if spec.kind is int:
                if key in positive:
                    _positive_int(values, key, default)
                elif key in nonnegative:
                    _nonnegative_int(values, key, default)
                elif key in bounded:
                    _bounded_int(values, key, default, bounded[key])
                elif key == "AVELIS_REDIS_PORT":
                    # 演示环境原样拼接此端口；只在新增分项连接方式中校验范围。
                    if values.get("AVELIS_REDIS_HOST"):
                        _bounded_int(values, key, default, 65535)
                else:
                    try:
                        int(values.get(key, str(default)))
                    except ValueError:
                        raise ValueError(f"{key} 必须为整数") from None
            elif spec.kind is float:
                if key in ("AVELIS_ANALYSIS_UNIFORM_TOLERANCE", "AVELIS_ANALYSIS_SPIKE_SHARE"):
                    _unit_interval(values, key, default)
                else:
                    try:
                        float(values.get(key, str(default)))
                    except ValueError:
                        raise ValueError(f"{key} 必须为数值") from None
            elif spec.kind is bool and key in (
                "AVELIS_AUTO_MIGRATE",
                "AVELIS_AUTO_INIT_ADMIN",
                "AVELIS_REDIS_SSL",
                "AVELIS_MCP_ENABLED",
            ):
                _boolean(values, key, default)
        except ValueError as exc:
            values.problem(key, str(exc))
    for key, default in (("AVELIS_HOST", Settings.host), ("AVELIS_FORWARDED_ALLOW_IPS", Settings.forwarded_allow_ips)):
        try:
            _nonempty_string(values, key, default)
        except ValueError as exc:
            values.problem(key, str(exc))
    try:
        _enum(values, "AVELIS_NUMBER_STYLE", Settings.number_style, ("cn", "intl"))
    except ValueError as exc:
        values.problem("AVELIS_NUMBER_STYLE", str(exc))
    try:
        _enum(
            values, "AVELIS_LOG_LEVEL", Settings.log_level, ("critical", "error", "warning", "info", "debug", "trace")
        )
    except ValueError as exc:
        values.problem("AVELIS_LOG_LEVEL", str(exc))
    try:
        _reply_language(values)
    except ValueError as exc:
        values.problem("AVELIS_REPLY_LANGUAGE", str(exc))
    if values.get("AVELIS_DB_SSLMODE", "").strip():
        try:
            _enum(
                values, "AVELIS_DB_SSLMODE", "", ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
            )
        except ValueError as exc:
            values.problem("AVELIS_DB_SSLMODE", str(exc))
    try:
        _frontend_directory(values)
    except ValueError as exc:
        values.problem("AVELIS_FRONTEND_DIR", str(exc))
    try:
        if any(c in values.get("AVELIS_PUBLIC_URL", "") for c in "?#"):
            raise ValueError("AVELIS_PUBLIC_URL 不支持查询参数或片段")
        if _boolean(values, "AVELIS_MCP_ENABLED", Settings.mcp_enabled):
            public_url = values.get("AVELIS_PUBLIC_URL", "").strip()
            parsed = urlsplit(public_url)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or any(c.isspace() for c in public_url)
                or "\\" in public_url
            ):
                raise ValueError("启用 MCP 时须填写无凭据、查询参数或片段的 HTTP(S) 公开地址")
            parsed.port  # 校验非法端口
    except ValueError as exc:
        values.problem("AVELIS_PUBLIC_URL", str(exc))
    if len(values.get("AVELIS_SECRET", "")) < 16:
        values.problem("AVELIS_SECRET", "必填，至少 16 个字符，当前为空或长度不足")
    if values.get("AVELIS_DATABASE_URL") and not values["AVELIS_DATABASE_URL"].startswith("postgresql"):
        values.problem("AVELIS_DATABASE_URL", "元数据库只支持 PostgreSQL，连接串须以 postgresql 开头")
    if not values.get("AVELIS_DATABASE_URL") and values.get("AVELIS_DB_HOST"):
        if not values.get("AVELIS_DB_PASSWORD", "").strip():
            values.problem("AVELIS_DB_PASSWORD", "必填，当前为空")
    if values.problems:
        raise ConfigError(values.problems, values.config_path)
    return _build_settings(values)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return build_settings()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="检查部署配置或导出内置服务口令")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="检查并显示脱敏后的生效配置")
    export = commands.add_parser("export-service-secrets", help="导出内置 PostgreSQL 和 Redis 的口令文件")
    export.add_argument("directory", type=Path)
    export.add_argument(
        "--services", default="postgres,redis", help="要导出口令的内置服务，逗号分隔；使用外部服务时留空，只校验配置"
    )
    args = parser.parse_args(argv)
    try:
        settings = build_settings()
        values = settings.env
        if args.command == "check":
            print(f"配置文件路径：{values.config_path}")
            for path, spec in CONFIG_KEYS.items():
                value = getattr(settings, spec.env.removeprefix("AVELIS_").lower(), values.get(spec.env, spec.default))
                if value is None:
                    value = values.get(spec.env, spec.default)
                if spec.env == "AVELIS_REDIS_PORT" and values.get("AVELIS_REDIS_HOST") and spec.env not in values:
                    value = 6379  # 分项连接未写端口时实际使用标准端口
                if spec.env in SENSITIVE_KEYS:
                    display = "******" if value else "（未设置）"
                elif value == "":
                    display = "（未设置）"
                elif isinstance(value, tuple):
                    display = ",".join(value)
                else:
                    display = flatten(value)
                source = values.sources.get(spec.env, "默认")
                if spec.env == "AVELIS_DATABASE_URL" and not values.get(spec.env) and values.get("AVELIS_DB_HOST"):
                    source = values.sources.get("AVELIS_DB_HOST", "默认") + "（由分项组装）"
                if spec.env == "AVELIS_REDIS_URL" and not values.get(spec.env) and values.get("AVELIS_REDIS_HOST"):
                    source = values.sources.get("AVELIS_REDIS_HOST", "默认") + "（由分项组装）"
                print(f"{path} | {spec.env} | {display} | 来源：{source}")
        else:
            # 取最终生效的连接串里的口令：分项、完整 URL 与文件引用都覆盖到，内置服务与应用用的一定是同一个口令
            wanted = {name.strip() for name in args.services.split(",") if name.strip()}
            unknown = wanted - {"postgres", "redis"}
            if unknown:
                raise ConfigError(
                    [f"--services 只支持 postgres、redis，收到：{', '.join(sorted(unknown))}"], values.config_path
                )
            secrets = {
                "postgres": ("postgres_password", "[database] password", settings.database_url),
                "redis": ("redis_password", "[redis] password", settings.redis_url),
            }
            missing = [
                f"{secrets[name][1]}：使用内置 {name} 服务时必填（用于初始化 / 启动该服务），当前为空。"
                f"请在配置文件 {values.config_path} 填写，或在连接串里带上口令"
                for name in sorted(wanted)
                if not urlsplit(secrets[name][2]).password
            ]
            if missing:
                raise ConfigError(missing, values.config_path)
            try:
                args.directory.mkdir(parents=True, exist_ok=True)
                for service in sorted(wanted):
                    name, _, url = secrets[service]
                    target = args.directory / name
                    # URL 里的口令是百分号编码：+ 是字面量，不能按表单编码当成空格
                    target.write_text(unquote(urlsplit(url).password), encoding="utf-8")
                    # 0644：内置 postgres、redis 以各自的非 root 用户读取；目录只挂在 Compose 的命名卷里
                    target.chmod(0o644)
                    print(f"已写入 {target}")
            except (OSError, ValueError):
                raise ConfigError(
                    [f"无法向目录 {args.directory} 写入口令文件，请检查路径与写入权限"], values.config_path
                ) from None
    except ConfigError as exc:
        report_config_error(exc)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
