"""问数、历史答案与依据、审计。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis.api.deps import current_user, get_session
from avelis.ask import service
from avelis.governance import access
from avelis.models import Answer, AuditEvent, User

router = APIRouter(prefix="/api", tags=["问数与审计"])


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


def answer_view(a: Answer) -> dict:
    return {"id": a.id, "question": a.question, "asked_by": a.asked_by, "asked_at": a.asked_at.isoformat(),
            "status": a.status, "message": a.message, "error_code": a.error_code, "model_adapter": a.model_adapter,
            "intent": a.intent, "plan": a.plan, "sql": a.sql, "metric_version_ids": a.metric_version_ids,
            "result": a.result, "evidence": a.evidence, "freshness": a.freshness,
            "executed_at": a.executed_at.isoformat() if a.executed_at else None, "duration_ms": a.duration_ms,
            "cache_hit": a.cache_hit, "recheck_status": a.recheck_status}


@router.post("/ask")
def ask(body: Question, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    return answer_view(service.ask(session, user, body.question))


@router.get("/answers")
def list_answers(all_users: bool = False, user: User = Depends(current_user),
                 session: Session = Depends(get_session)) -> list[dict]:
    stmt = select(Answer).order_by(Answer.asked_at.desc()).limit(200)
    if not (all_users and access.is_admin(session, user)):
        stmt = stmt.where(Answer.asked_by == user.id)
    return [answer_view(a) for a in session.scalars(stmt)]


@router.get("/answers/{answer_id}")
def get_answer(answer_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    a = session.get(Answer, answer_id)
    if a is None or (a.asked_by != user.id and not access.is_admin(session, user)):
        raise HTTPException(404, "答案不存在或无权访问")
    return answer_view(a)


@router.get("/audit")
def list_audit(object_type: str | None = None, object_id: str | None = None, action: str | None = None,
               limit: int = 200, user: User = Depends(current_user), session: Session = Depends(get_session)):
    access.require_role(session, user, "steward", "reviewer")
    stmt = select(AuditEvent).order_by(AuditEvent.at.desc()).limit(min(limit, 1000))
    if object_type:
        stmt = stmt.where(AuditEvent.object_type == object_type)
    if object_id:
        stmt = stmt.where(AuditEvent.object_id == object_id)
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    return [{"id": e.id, "at": e.at.isoformat(), "actor_id": e.actor_id, "actor": e.actor_name, "action": e.action,
             "object_type": e.object_type, "object_id": e.object_id, "detail": e.detail}
            for e in session.scalars(stmt)]
