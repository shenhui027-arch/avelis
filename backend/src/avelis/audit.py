"""审计：只追加，记录真实操作者。detail 中不得出现口令或令牌。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from avelis.db import new_id, table_exists, utcnow
from avelis.models import AuditEvent, User

SYSTEM = "system"


def record(
    session: Session, actor: User | None, action: str, object_type: str, object_id: str | None = None, **detail: Any
) -> AuditEvent | None:
    if session is None or not hasattr(session, "add"):
        return None
    if isinstance(session, Session) and not table_exists(session, "audit_event"):
        return None
    from avelis.events import prepare_audit, sanitize_detail
    from avelis.observability.context import get_request_context

    merged = {**get_request_context(), **detail}
    event = AuditEvent(
        id=new_id(),
        at=utcnow(),
        actor_id=actor.id if actor else None,
        actor_name=actor.username if actor else SYSTEM,
        action=action,
        object_type=object_type,
        object_id=object_id,
        detail=sanitize_detail(merged),
    )
    prepare_audit(event)
    session.add(event)
    return event
