"""TOML 映射、配置来源与敏感文件引用；只使用标准库。"""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass
from difflib import get_close_matches
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("/etc/avelis/avelis.toml")


@dataclass(frozen=True)
class ConfigKey:
    env: str
    kind: type
    default: object


# 一处列明部署配置的合法键；默认值与现有环境变量解析保持一致。
CONFIG_KEYS = {
    "server.host": ConfigKey("AVELIS_HOST", str, "127.0.0.1"),
    "server.port": ConfigKey("AVELIS_PORT", int, 8000),
    "server.workers": ConfigKey("AVELIS_WORKERS", int, 1),
    "server.public_url": ConfigKey("AVELIS_PUBLIC_URL", str, ""),
    "server.forwarded_allow_ips": ConfigKey("AVELIS_FORWARDED_ALLOW_IPS", str, "127.0.0.1"),
    "server.frontend_dir": ConfigKey("AVELIS_FRONTEND_DIR", str, ""),
    "server.log_level": ConfigKey("AVELIS_LOG_LEVEL", str, "info"),
    "server.cors_origins": ConfigKey("AVELIS_CORS_ORIGINS", list, ["http://127.0.0.1:5173", "http://localhost:5173"]),
    "mcp.enabled": ConfigKey("AVELIS_MCP_ENABLED", bool, True),
    "mcp.registration_max_per_hour": ConfigKey("AVELIS_MCP_REGISTRATION_MAX_PER_HOUR", int, 20),
    "mcp.consent_max_per_hour": ConfigKey("AVELIS_MCP_CONSENT_MAX_PER_HOUR", int, 120),
    "mcp.max_pending_clients": ConfigKey("AVELIS_MCP_MAX_PENDING_CLIENTS", int, 1000),
    "mcp.pending_client_days": ConfigKey("AVELIS_MCP_PENDING_CLIENT_DAYS", int, 7),
    "mcp.replay_retention_days": ConfigKey("AVELIS_MCP_REPLAY_RETENTION_DAYS", int, 7),
    "mcp.cleanup_batch_size": ConfigKey("AVELIS_MCP_CLEANUP_BATCH_SIZE", int, 500),
    "mcp.access_token_seconds": ConfigKey("AVELIS_MCP_ACCESS_TOKEN_SECONDS", int, 900),
    "mcp.refresh_token_seconds": ConfigKey("AVELIS_MCP_REFRESH_TOKEN_SECONDS", int, 2592000),
    "mcp.cimd_cache_ttl_seconds": ConfigKey("AVELIS_MCP_CIMD_CACHE_TTL_SECONDS", int, 3600),
    "mcp.cimd_allowed_urls": ConfigKey("AVELIS_MCP_CIMD_ALLOWED_URLS", list, []),
    "security.secret": ConfigKey("AVELIS_SECRET", str, ""),
    "security.access_token_minutes": ConfigKey("AVELIS_ACCESS_TOKEN_MINUTES", int, 120),
    "security.session_max_hours": ConfigKey("AVELIS_SESSION_MAX_HOURS", int, 168),
    "security.login_max_failures": ConfigKey("AVELIS_LOGIN_MAX_FAILURES", int, 5),
    "security.login_lock_minutes": ConfigKey("AVELIS_LOGIN_LOCK_MINUTES", int, 15),
    "security.allow_self_review": ConfigKey("AVELIS_ALLOW_SELF_REVIEW", bool, False),
    "database.url": ConfigKey("AVELIS_DATABASE_URL", str, ""),
    "database.host": ConfigKey("AVELIS_DB_HOST", str, ""),
    "database.port": ConfigKey("AVELIS_DB_PORT", int, 5432),
    "database.name": ConfigKey("AVELIS_DB_NAME", str, "avelis"),
    "database.user": ConfigKey("AVELIS_DB_USER", str, "avelis"),
    "database.password": ConfigKey("AVELIS_DB_PASSWORD", str, ""),
    "database.sslmode": ConfigKey("AVELIS_DB_SSLMODE", str, ""),
    "database.pool_size": ConfigKey("AVELIS_DB_POOL_SIZE", int, 5),
    "database.max_overflow": ConfigKey("AVELIS_DB_MAX_OVERFLOW", int, 10),
    "database.pool_recycle_seconds": ConfigKey("AVELIS_DB_POOL_RECYCLE_SECONDS", int, 1800),
    "redis.url": ConfigKey("AVELIS_REDIS_URL", str, ""),
    "redis.host": ConfigKey("AVELIS_REDIS_HOST", str, ""),
    "redis.port": ConfigKey("AVELIS_REDIS_PORT", int, 16379),
    "redis.db": ConfigKey("AVELIS_REDIS_DB", int, 0),
    "redis.password": ConfigKey("AVELIS_REDIS_PASSWORD", str, ""),
    "redis.ssl": ConfigKey("AVELIS_REDIS_SSL", bool, False),
    "bootstrap.auto_migrate": ConfigKey("AVELIS_AUTO_MIGRATE", bool, True),
    "bootstrap.auto_init_admin": ConfigKey("AVELIS_AUTO_INIT_ADMIN", bool, True),
    "bootstrap.initial_admin_username": ConfigKey("AVELIS_INITIAL_ADMIN_USERNAME", str, "admin"),
    "bootstrap.initial_admin_password": ConfigKey("AVELIS_INITIAL_ADMIN_PASSWORD", str, ""),
    "business.timezone": ConfigKey("AVELIS_BUSINESS_TIMEZONE", str, "Asia/Shanghai"),
    "business.number_style": ConfigKey("AVELIS_NUMBER_STYLE", str, "cn"),
    "business.unassigned_asset_policy": ConfigKey("AVELIS_UNASSIGNED_ASSET_POLICY", str, "deny"),
    "business.require_column_comments": ConfigKey("AVELIS_REQUIRE_COLUMN_COMMENTS", bool, False),
    "query.timeout_seconds": ConfigKey("AVELIS_QUERY_TIMEOUT_SECONDS", int, 30),
    "query.mem_limit_bytes": ConfigKey("AVELIS_QUERY_MEM_LIMIT_BYTES", int, 2147483648),
    "query.max_rows": ConfigKey("AVELIS_QUERY_MAX_ROWS", int, 1000),
    "query.answer_cache_seconds": ConfigKey("AVELIS_ANSWER_CACHE_SECONDS", int, 300),
    "query.live_check": ConfigKey("AVELIS_ASK_LIVE_CHECK", bool, True),
    "query.max_tool_rounds": ConfigKey("AVELIS_ASK_MAX_TOOL_ROUNDS", int, 8),
    "agent.reply_language": ConfigKey("AVELIS_REPLY_LANGUAGE", str, "auto"),
    "collect.interval_minutes": ConfigKey("AVELIS_COLLECT_INTERVAL_MINUTES", int, 15),
    "collect.ddl_audit_slack_seconds": ConfigKey("AVELIS_DDL_AUDIT_SLACK_SECONDS", int, 5),
    "worker.schedule_seconds": ConfigKey("AVELIS_WORKER_SCHEDULE_SECONDS", int, 60),
    "worker.stale_seconds": ConfigKey("AVELIS_WORKER_STALE_SECONDS", int, 30),
    "worker.running_timeout_seconds": ConfigKey("AVELIS_WORKER_RUNNING_TIMEOUT_SECONDS", int, 3600),
    "webhook.allow_private_urls": ConfigKey("AVELIS_WEBHOOK_ALLOW_PRIVATE_URLS", bool, False),
    "webhook.max_attempts": ConfigKey("AVELIS_WEBHOOK_MAX_ATTEMPTS", int, 8),
    "webhook.retry_max_minutes": ConfigKey("AVELIS_WEBHOOK_RETRY_MAX_MINUTES", int, 60),
    "events.delivered_retention_days": ConfigKey("AVELIS_EVENTS_DELIVERED_RETENTION_DAYS", int, 30),
    "history.retention_days": ConfigKey("AVELIS_HISTORY_RETENTION_DAYS", int, 1095),
    "history.purge_interval_hours": ConfigKey("AVELIS_HISTORY_PURGE_INTERVAL_HOURS", int, 24),
    "history.export_max_rows": ConfigKey("AVELIS_HISTORY_EXPORT_MAX_ROWS", int, 50000),
    "analysis.query_limit": ConfigKey("AVELIS_ANALYSIS_QUERY_LIMIT", int, 20),
    "analysis.tool_calls": ConfigKey("AVELIS_ANALYSIS_TOOL_CALLS", int, 30),
    "analysis.seconds": ConfigKey("AVELIS_ANALYSIS_SECONDS", int, 300),
    "analysis.explore_limit": ConfigKey("AVELIS_ANALYSIS_EXPLORE_LIMIT", int, 8),
    "analysis.model_requests": ConfigKey("AVELIS_ANALYSIS_MODEL_REQUESTS", int, 16),
    "analysis.input_tokens": ConfigKey("AVELIS_ANALYSIS_INPUT_TOKENS", int, 552321),
    "analysis.output_tokens": ConfigKey("AVELIS_ANALYSIS_OUTPUT_TOKENS", int, 30716),
    "analysis.material_change_ratio": ConfigKey("AVELIS_MATERIAL_CHANGE_RATIO", float, 0.01),
    "analysis.retrieval": ConfigKey("AVELIS_ANALYSIS_RETRIEVAL", bool, True),
    "analysis.investigation": ConfigKey("AVELIS_ANALYSIS_INVESTIGATION", bool, True),
    "analysis.uniform_tolerance": ConfigKey("AVELIS_ANALYSIS_UNIFORM_TOLERANCE", float, 0.5),
    "analysis.spike_share": ConfigKey("AVELIS_ANALYSIS_SPIKE_SHARE", float, 0.5),
    "model.timeout_seconds": ConfigKey("AVELIS_MODEL_TIMEOUT_SECONDS", int, 60),
    "model.provider": ConfigKey("AVELIS_MODEL_PROVIDER", str, ""),
    "model.name": ConfigKey("AVELIS_MODEL_NAME", str, ""),
    "model.base_url": ConfigKey("AVELIS_MODEL_BASE_URL", str, ""),
    "model.api_key": ConfigKey("AVELIS_MODEL_API_KEY", str, ""),
    "model.send_results": ConfigKey("AVELIS_MODEL_SEND_RESULTS", bool, False),
    "import.max_bytes": ConfigKey("AVELIS_IMPORT_MAX_BYTES", int, 20 * 1024**2),  # 单文件或粘贴内容，20 MB
    "import.max_batch_bytes": ConfigKey("AVELIS_IMPORT_MAX_BATCH_BYTES", int, 100 * 1024**2),
}
ENV_KEYS = {spec.env: path for path, spec in CONFIG_KEYS.items()}
SENSITIVE_KEYS = frozenset(
    {
        "AVELIS_SECRET",
        "AVELIS_DB_PASSWORD",
        "AVELIS_REDIS_PASSWORD",
        "AVELIS_INITIAL_ADMIN_PASSWORD",
        "AVELIS_MODEL_API_KEY",
        "AVELIS_DATABASE_URL",
        "AVELIS_REDIS_URL",
    }
)


class ConfigError(ValueError, RuntimeError):
    """同时兼容已有 ValueError 与缺少密钥时的 RuntimeError 捕获。"""

    def __init__(self, problems: list[str], config_path: Path | None = None):
        self.problems = problems
        self.config_path = config_path
        super().__init__("\n".join(problems))


class ConfigValues(dict[str, str]):
    """保持字典接口，附带来源与尚未汇总抛出的加载问题。"""

    def __init__(self, values=(), *, source="环境变量", config_path: Path | None = None):
        super().__init__(values)
        self.sources = dict.fromkeys(self, source)
        self.problems: list[str] = []
        self.file_problems: dict[str, list[str]] = {}  # 文件引用读不到：只有该层是最终取值层时才算错误
        self.config_path = config_path

    def problem(self, key: str, message: str, *, source: str | None = None) -> None:
        path = ENV_KEYS.get(key)
        label = f"[{path.split('.')[0]}] {path.split('.')[1]}" if path else key
        origin = source or self.sources.get(key, "默认")
        file_option = f" / {key}_FILE" if key in SENSITIVE_KEYS else ""
        self.problems.append(
            f"{label}（环境变量 {key}；来源：{origin}）：{message}。"
            f"请在配置文件 {self.config_path or DEFAULT_CONFIG_PATH} 填写，或设置环境变量 {key}{file_option}"
        )


def flatten(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(value)
    return str(value)


def read_config(process: dict[str, str]) -> ConfigValues:
    explicit = "AVELIS_CONFIG" in process
    path = Path(process["AVELIS_CONFIG"]) if explicit else DEFAULT_CONFIG_PATH
    result = ConfigValues(config_path=path)
    try:
        if not explicit and not path.exists():
            return result
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # TOML 异常可能带原始输入，不回显解析器异常或敏感值。
        result.problem(
            "AVELIS_CONFIG", "配置文件不存在、无法读取或 TOML 格式无效", source="环境变量" if explicit else "默认"
        )
        return result
    sections = {p.split(".")[0] for p in CONFIG_KEYS}
    valid = dict(CONFIG_KEYS)
    for name, spec in CONFIG_KEYS.items():
        if spec.env in SENSITIVE_KEYS:
            valid[f"{name}_file"] = ConfigKey(f"{spec.env}_FILE", str, "")
    for section, entries in data.items():
        if section not in sections or not isinstance(entries, dict):
            suggestion = get_close_matches(section, sorted(sections), n=1, cutoff=0)
            hint = f"；建议使用 [{suggestion[0]}]" if suggestion else ""
            result.problems.append(
                f"[{section}]（来源：配置文件 {path}）：未知的节或节格式无效{hint}。"
                "合法的节见 avelis.example.toml 或 docs/configuration.md"
            )
            continue
        for name, value in entries.items():
            full = f"{section}.{name}"
            spec = valid.get(full)
            if spec is None:
                suggestion = get_close_matches(full, valid, n=1, cutoff=0)
                hint = f"；建议使用 {suggestion[0]}" if suggestion else ""
                result.problems.append(
                    f"[{section}] {name}（来源：配置文件 {path}）：未知的键{hint}。"
                    "合法的键见 avelis.example.toml 或 docs/configuration.md"
                )
                continue
            correct = type(value) is spec.kind
            if spec.kind is float:
                correct = type(value) in (int, float)
            if spec.kind is list:
                correct = isinstance(value, list) and all(type(item) is str for item in value)
            if not correct:
                expected = {
                    str: "字符串（加英文双引号）",
                    int: "整数（不加引号）",
                    float: "数值",
                    bool: "true 或 false（不加引号）",
                    list: '字符串数组，如 ["a", "b"]',
                }[spec.kind]
                result.problem(spec.env.removesuffix("_FILE"), f"类型不符，必须为 {expected}", source="配置文件")
                continue
            result[spec.env] = flatten(value)
            result.sources[spec.env] = "配置文件"
            # 环境变量层保留历史的宽松策略值；新 TOML 输入只接受明确的枚举。
            if spec.env == "AVELIS_UNASSIGNED_ASSET_POLICY" and value.strip().lower() not in ("allow", "deny"):
                result.problem(spec.env, "必须为 allow 或 deny", source="配置文件")
    return result


def resolve_files(layer: ConfigValues) -> ConfigValues:
    """逐层解析，避免高层明文与低层文件引用被误判为同层冲突。"""
    for key in sorted(SENSITIVE_KEYS):
        file_key = f"{key}_FILE"
        if file_key not in layer:
            continue
        source = layer.sources.get(file_key, "环境变量")
        if key in layer:
            layer.problem(key, f"同一层不能同时设置 {key} 和 {file_key}", source=source)
            continue
        try:
            layer[key] = Path(layer[file_key]).read_text(encoding="utf-8").strip()
            layer.sources[key] = source
        except (OSError, ValueError):
            before = len(layer.problems)
            layer.problem(key, f"无法读取 {file_key} 指向的文件 {layer[file_key]}", source=source)
            layer.file_problems[key] = layer.problems[before:]
            del layer.problems[before:]
    return layer


def merge_layers(*layers: ConfigValues) -> ConfigValues:
    result = ConfigValues(config_path=layers[0].config_path)
    winner: dict[str, ConfigValues] = {}
    for layer in layers:
        result.update(layer)
        result.sources.update(layer.sources)
        result.problems.extend(layer.problems)
        for key in SENSITIVE_KEYS:
            if key in layer or key in layer.file_problems:
                winner[key] = layer
    # 低层失效的文件引用被高层覆盖时不再报错
    for key, layer in winner.items():
        result.problems.extend(layer.file_problems.get(key, []))
    return result


def report_config_error(exc: ConfigError) -> None:
    print(f"配置有误（共 {len(exc.problems)} 项）：", file=sys.stderr)
    for problem in exc.problems:
        print(f"- {problem}", file=sys.stderr)
    print(f"配置文件路径：{exc.config_path or DEFAULT_CONFIG_PATH}", file=sys.stderr)
