"""审计：只追加，记录真实操作者。detail 中不得出现口令或令牌。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from avelis.db import new_id, utcnow
from avelis.models import AuditEvent, User

SYSTEM = "system"


def record(
    session: Session, actor: User | None, action: str, object_type: str, object_id: str | None = None, **detail: Any
) -> AuditEvent:
    from avelis.events import prepare_audit, sanitize_detail

    event = AuditEvent(
        id=new_id(),
        at=utcnow(),
        actor_id=actor.id if actor else None,
        actor_name=actor.username if actor else SYSTEM,
        action=action,
        object_type=object_type,
        object_id=object_id,
        detail=sanitize_detail(detail),
    )
    prepare_audit(event)
    session.add(event)
    return event
