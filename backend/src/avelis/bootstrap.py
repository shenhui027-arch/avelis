"""初始化命令。

  uv run python -m avelis.bootstrap migrate        迁移元数据库到最新版本
  uv run python -m avelis.bootstrap admin          创建首个管理员 admin（口令取自 AVELIS_DEMO_USER_PASSWORD）

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


def main(argv: list[str]) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = argv[0] if argv else ""
    if cmd == "migrate":
        command.upgrade(alembic_config(), "head")
        print("元数据库已迁移到最新版本")
    elif cmd == "admin":
        ensure_admin()
        print("管理员 admin 已就绪（口令见 .env 的 AVELIS_DEMO_USER_PASSWORD，未打印）")
    else:
        print(__doc__)
        raise SystemExit(2)


if __name__ == "__main__":
    main(sys.argv[1:])
