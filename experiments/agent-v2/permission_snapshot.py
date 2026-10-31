"""权限快照（manifest b_raw.permission_snapshot）：评测账号 → 角色 → 可访问的业务域，输出 JSON 与 SHA-256。只读元数据库。

  AVELIS_DATABASE_URL=<私有元数据库> uv run --project backend python experiments/agent-v2/permission_snapshot.py wang sun

E1 / E2 每轮运行前后各导出一次；两次哈希不同说明运行期间权限变了，这一轮的结果要标注。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "backend" / "src")]


def main(usernames: list[str]) -> int:
    from sqlalchemy import select

    from avelis.db import session_scope
    from avelis.governance import access
    from avelis.models import Domain, User

    snapshot = {}
    with session_scope() as session:
        for name in usernames:
            user = session.scalar(select(User).where(User.username == name))
            if user is None:
                snapshot[name] = None
                continue
            domains = sorted(filter(None, (getattr(session.get(Domain, d), "name", None)
                                           for d in access.domain_ids(session, user))))
            snapshot[name] = {"active": bool(user.is_active), "roles": sorted(access.roles_of(session, user)),
                              "domains": domains}
    text = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    print(json.dumps({"snapshot": snapshot, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["wang", "sun"]))
