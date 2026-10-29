"""初始化命令。

  uv run python -m avelis.bootstrap migrate        迁移元数据库到最新版本
  uv run python -m avelis.bootstrap admin          创建首个管理员 admin（口令取自 AVELIS_DEMO_USER_PASSWORD）
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
from avelis.db import session_scope
from avelis.devtools import alembic_config
from avelis.models import User, UserRole
from avelis.security import hash_password


def ensure_admin(username: str = "admin", display_name: str = "管理员") -> str:
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
    admin_id = ensure_admin()
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
        ensure_admin()
        print("管理员 admin 已就绪（口令见 .env 的 AVELIS_DEMO_USER_PASSWORD，未打印）")
    else:
        print(__doc__)
        raise SystemExit(2)


if __name__ == "__main__":
    main(sys.argv[1:])
