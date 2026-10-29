"""初始化命令。

  uv run python -m avelis.bootstrap migrate        迁移元数据库到最新版本
  uv run python -m avelis.bootstrap admin          创建自动化管理员 ops_admin（密码取自 AVELIS_DEMO_USER_PASSWORD），
                                                   并按 AVELIS_ADMIN_PASSWORD 初始化本地超级管理员 admin（重设其密码）
  uv run python -m avelis.bootstrap demo [--reset] [--metrics]  演示走查用：经 API 建好用户、业务域、数据源并采集
  uv run python -m avelis.bootstrap openapi PATH   导出 OpenAPI 文档（前端据此生成类型）
  uv run python -m avelis.bootstrap model          按 .env 的 AVELIS_MODEL_* 登记一个模型提供方并设为问数模型
                                                   （之后在“模型配置”页面维护）
  uv run python -m avelis.bootstrap verify-history 核验历史哈希链（断链时退出码 1，打印位置与原因）
  uv run python -m avelis.bootstrap purge-history [--dry-run] [--days N]
                                                   按保留期清理超期历史（默认 AVELIS_HISTORY_RETENTION_DAYS）

其余对象（用户、业务域、数据源、指标）都通过界面或 API 维护，不需要手改数据库。
"""

from __future__ import annotations

import sys

from alembic import command
from sqlalchemy import select

from avelis.config import get_settings
from avelis.db import session_scope, utcnow
from avelis.devtools import alembic_config
from avelis.governance.access import ROLES
from avelis.models import User, UserRole
from avelis.security import hash_password, session_cutoff_now, verify_password

# 演示准备与验收脚本经 API 操作时使用的管理员；不占用给人登录的 admin
AUTOMATION_ADMIN = "ops_admin"
SUPERADMIN = "admin"


def ensure_admin(username: str = AUTOMATION_ADMIN, display_name: str = "自动化管理员", *,
                 superadmin: bool = True) -> str:
    """自动化管理员（演示准备、验收脚本用）；superadmin=True 时同时补齐本地超级管理员。"""
    password = get_settings().env.get("AVELIS_DEMO_USER_PASSWORD", "")
    if len(password) < 8:
        raise SystemExit("AVELIS_DEMO_USER_PASSWORD 未设置或过短；运行 uv run scripts/demo.py init-env")
    with session_scope() as session:
        user = session.scalar(select(User).where(User.username == username))
        if user is None:
            user = User(username=username, display_name=display_name, password_hash=hash_password(password))
            session.add(user)
            session.flush()
            session.add(UserRole(user_id=user.id, role="admin"))
        user_id = user.id
    if superadmin:
        ensure_superadmin()
    return user_id


LOCAL_ONLY = "本地超级管理员只用于本机元数据库（回环地址或本机 socket）；当前连接的元数据库不在本机，已拒绝"


def is_local_target(host: str | None, hostaddr: str | None) -> bool:
    """连接实际使用的目标是否在本机：TCP 连接看实际的 IP（hostaddr），Unix socket 连接的 host 是 socket 目录。"""
    import ipaddress

    if hostaddr:
        try:
            return ipaddress.ip_address(hostaddr.strip("[]")).is_loopback
        except ValueError:
            return False
    return bool(host) and host.startswith("/")


def _connection_is_local(session) -> bool:
    """读这条连接实际连上的地址（libpq 解析完连接串、服务文件、环境变量与多主机之后的结果），
    而不是解析连接串：连接串的写法太多，解析难免与驱动不一致。"""
    from sqlalchemy import text

    info = session.connection().connection.driver_connection.info
    hostaddr = getattr(info, "hostaddr", None)
    if not is_local_target(info.host, hostaddr):
        return False
    if not hostaddr:
        # 驱动报告为 socket 连接：再由服务器确认（TCP 连接的 inet_server_addr() 一定非空），
        # 避免 libpq 未能记录实际 IP 时把远端 TCP 误当作本机 socket
        return session.scalar(text("SELECT inet_server_addr() IS NULL")) is True
    return True


def ensure_superadmin(reset_password: bool = False) -> str | None:
    """本地超级管理员 admin：拥有全部角色，密码取自 AVELIS_ADMIN_PASSWORD；未配置时不创建。

    只用于本地开发（用户决定，2026-10-29）：这里直接写入密码，不经过界面的密码策略校验，
    所以允许 123456 这样的弱密码。只在元数据库位于本机时执行：判断用的是执行写入的同一条连接的实际地址；
    非本机时，补齐调用直接跳过，显式重设（bootstrap admin）报错退出，都不写入。

    已存在时只补齐角色，不改启用状态、首次改密标记与密码；reset_password=True 时恢复为可用状态，
    并把密码重设为配置值。"""
    password = get_settings().env.get("AVELIS_ADMIN_PASSWORD", "")
    if not password:
        return None
    with session_scope() as session:
        if not _connection_is_local(session):
            if reset_password:
                raise SystemExit(LOCAL_ONLY)
            return None
        user = session.scalar(select(User).where(User.username == SUPERADMIN))
        if user is None:
            user = User(username=SUPERADMIN, display_name="超级管理员", password_hash=hash_password(password))
            session.add(user)
            session.flush()
        elif reset_password:
            if not verify_password(password, user.password_hash):
                user.password_hash = hash_password(password)
                user.password_changed_at = utcnow()
                user.sessions_valid_after = session_cutoff_now()  # 旧令牌失效，随后签发的新令牌有效
            user.is_active = True
            user.must_change_password = False
        have = set(session.scalars(select(UserRole.role).where(UserRole.user_id == user.id)))
        for role in ROLES:
            if role not in have:
                session.add(UserRole(user_id=user.id, role=role))
        return user.id


def model_from_env() -> str:
    """开发便利：把 .env 中的 AVELIS_MODEL_* 登记为提供方，并指定为问数理解与导入推断模型。"""
    from avelis import modelcfg
    from avelis.models import ModelProvider

    env = get_settings().env
    provider, name, key = (env.get(k, "").strip() for k in ("AVELIS_MODEL_PROVIDER", "AVELIS_MODEL_NAME",
                                                            "AVELIS_MODEL_API_KEY"))
    if not (provider and name and key):
        raise SystemExit("缺少 AVELIS_MODEL_PROVIDER / AVELIS_MODEL_NAME / AVELIS_MODEL_API_KEY")
    admin_id = ensure_admin(superadmin=False)  # 登记模型不需要本地超级管理员
    with session_scope() as session:
        admin = session.get(User, admin_id)
        p = session.scalar(select(ModelProvider).where(ModelProvider.code == "env-default"))
        kind = "anthropic" if provider == "anthropic" else "openai_compatible"
        if p is None:
            p = modelcfg.create_provider(session, admin, code="env-default", preset="custom",
                                         name=f"{provider}（来自 .env）", kind=kind,
                                         base_url=env.get("AVELIS_MODEL_BASE_URL") or None, api_key=key,
                                         models=[{"id": name, "enabled": True}])
        modelcfg.check(session, admin, p.id, name)
        modelcfg.save_settings(session, admin, {"ask": {"provider_id": p.id, "model_id": name},
                                                "import": {"provider_id": p.id, "model_id": name}}, None)
        return p.id


def main(argv: list[str]) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = argv[0] if argv else ""
    if cmd == "migrate":
        command.upgrade(alembic_config(), "head")
        print("元数据库已迁移到最新版本")
    elif cmd == "demo":
        from avelis.demo_setup import setup

        setup(reset="--reset" in argv, metrics="--metrics" in argv)
        if get_settings().env.get("AVELIS_MODEL_API_KEY"):
            model_from_env()
            print("已按 .env 登记模型提供方（密钥加密存储，未打印）")
    elif cmd == "openapi":
        import json
        from pathlib import Path

        from avelis.api.app import create_app

        target = Path(argv[1])
        spec = json.dumps(create_app().openapi(), ensure_ascii=False, indent=1)
        target.write_text(spec + chr(10), encoding="utf-8")
        print(f"已导出 {target}")
    elif cmd == "model":
        model_from_env()
        print("已按 .env 登记模型提供方并设为问数理解、导入推断模型（密钥已加密存储，未打印）")
    elif cmd == "verify-history":
        import json

        from avelis import audit
        from avelis.db import session_factory
        from avelis.history import integrity

        session = session_factory()()
        try:
            session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            report = integrity.verify(session)
        finally:
            session.rollback()
            session.close()
        with session_scope() as session:
            audit.record(session, None, "history.verify", "history", None, ok=report["ok"], via="cli",
                         issue_count=report["issue_count"], head=report["head"], first_break=report["first_break"])
        print(json.dumps(report, ensure_ascii=False, indent=1))
        raise SystemExit(0 if report["ok"] else 1)
    elif cmd == "purge-history":
        import json

        from avelis.history import retention

        days = int(argv[argv.index("--days") + 1]) if "--days" in argv else None
        try:
            result = retention.purge(retention_days=days, dry_run="--dry-run" in argv)
        except retention.PurgeRefused as exc:
            print(json.dumps(exc.report, ensure_ascii=False, indent=1))
            raise SystemExit("历史哈希链核验未通过，未执行清理") from exc
        print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    elif cmd == "admin":
        if get_settings().env.get("AVELIS_ADMIN_PASSWORD"):
            # 先检查：连到的元数据库不在本机就直接拒绝，不写入任何账号。随后每个写入步骤用的是各自的连接，
            # 写 admin 时还会在那条连接上再检查一次
            with session_scope() as session:
                if not _connection_is_local(session):
                    raise SystemExit(LOCAL_ONLY)
        ensure_admin()
        ready = ensure_superadmin(reset_password=True)
        print(f"自动化管理员 {AUTOMATION_ADMIN} 已就绪（密码见 .env 的 AVELIS_DEMO_USER_PASSWORD，未打印）")
        print(f"超级管理员 {SUPERADMIN} 已就绪，拥有全部角色（密码见 .env 的 AVELIS_ADMIN_PASSWORD，未打印）" if ready
              else "未配置 AVELIS_ADMIN_PASSWORD，未创建超级管理员 admin")
    else:
        print(__doc__)
        raise SystemExit(2)


if __name__ == "__main__":
    main(sys.argv[1:])
