"""部署启动入口：uv run python -m avelis.server。

AVELIS_HOST / AVELIS_PORT / AVELIS_WORKERS 控制监听地址、端口与进程数；
AVELIS_FORWARDED_ALLOW_IPS 指定可信代理，AVELIS_LOG_LEVEL 控制服务日志级别。
AVELIS_AUTO_MIGRATE=true 时先迁移并建立智能体状态表；
AVELIS_AUTO_INIT_ADMIN=true 时按 AVELIS_INITIAL_ADMIN_USERNAME（默认 admin）与
AVELIS_INITIAL_ADMIN_PASSWORD 创建首个管理员，未配置或口令无效时记录日志并跳过。
两个自动初始化开关默认关闭；前端托管由 AVELIS_FRONTEND_DIR 单独控制。
"""

from __future__ import annotations

import logging

import uvicorn

from avelis.bootstrap import InitialAdminError, init_admin, migrate
from avelis.config import Settings, get_settings

logger = logging.getLogger(__name__)


def uvicorn_options(settings: Settings) -> dict:
    """组装服务参数，不创建应用或连接外部服务。"""
    return {"host": settings.host, "port": settings.port, "workers": settings.workers,
            "proxy_headers": True, "forwarded_allow_ips": settings.forwarded_allow_ips,
            "log_level": settings.log_level}


def main() -> None:
    from avelis.config import ConfigError, report_config_error

    try:
        settings = get_settings()
    except ConfigError as exc:
        report_config_error(exc)
        raise SystemExit(2) from None
    logging.basicConfig(level=logging.DEBUG if settings.log_level == "trace" else settings.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if settings.auto_migrate:
        migrate()
    if settings.auto_init_admin:
        if not settings.env.get("AVELIS_INITIAL_ADMIN_PASSWORD"):
            logger.warning("未配置 AVELIS_INITIAL_ADMIN_PASSWORD，已跳过初始管理员创建")
        else:
            try:
                init_admin()
            except InitialAdminError as exc:
                logger.error("初始管理员创建已跳过：%s", exc)
            except Exception:
                # 驱动异常可能带连接详情；启动日志只记录失败项。
                logger.error("初始管理员创建失败，已跳过；请检查元数据库状态")
    logger.info("监听地址：%s:%s", settings.host, settings.port)
    if settings.public_url:
        logger.info("访问地址：%s", settings.public_url)
    uvicorn.run("avelis.api.app:app", **uvicorn_options(settings))


if __name__ == "__main__":
    main()
