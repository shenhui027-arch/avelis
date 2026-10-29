"""问数、历史答案与依据、审计。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis.api import schemas as S
from avelis.api.deps import current_user, get_session
from avelis.ask import agent
from avelis.governance import access
from avelis.models import Answer, AskThread, AuditEvent, User

router = APIRouter(prefix="/api", tags=["问数与审计"])


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    thread_id: str | None = Field(default=None, description="继续已有会话（含回答追问）；为空则新建会话")


def answer_view(a: Answer) -> dict:
    return {"id": a.id, "question": a.question, "asked_by": a.asked_by, "asked_at": a.asked_at.isoformat(),
            "status": a.status, "message": a.message, "error_code": a.error_code, "model_adapter": a.model_adapter,
            "intent": a.intent, "plan": a.plan, "sql": a.sql, "metric_version_ids": a.metric_version_ids,
            "result": a.result, "evidence": a.evidence, "freshness": a.freshness,
            "executed_at": a.executed_at.isoformat() if a.executed_at else None, "duration_ms": a.duration_ms,
            "cache_hit": a.cache_hit, "recheck_status": a.recheck_status, "thread_id": a.thread_id,
            "steps": a.steps, "explanation": a.explanation, "explanation_source": a.explanation_source}


@router.post("/ask", response_model=S.AnswerOut)
def ask(body: Question, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    return answer_view(agent.run_turn(session, user, body.question, body.thread_id))


@router.get("/ask/threads", response_model=list[S.ThreadOut])
def list_threads(user: User = Depends(current_user), session: Session = Depends(get_session)) -> list[dict]:
    return [{"id": t.id, "title": t.title, "status": t.status, "created_at": t.created_at.isoformat(),
             "updated_at": t.updated_at.isoformat()} for t in session.scalars(
        select(AskThread).where(AskThread.user_id == user.id).order_by(AskThread.updated_at.desc()).limit(100))]


@router.get("/ask/threads/{thread_id}", response_model=S.ThreadDetailOut)
def get_thread(thread_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    t = session.get(AskThread, thread_id)
    if t is None or t.user_id != user.id:
        raise HTTPException(404, "会话不存在")
    answers = session.scalars(select(Answer).where(Answer.thread_id == t.id).order_by(Answer.asked_at))
    return {"id": t.id, "title": t.title, "status": t.status, "answers": [answer_view(a) for a in answers]}


@router.get("/answers", response_model=list[S.AnswerOut])
def list_answers(all_users: bool = False, user: User = Depends(current_user),
                 session: Session = Depends(get_session)) -> list[dict]:
    stmt = select(Answer).order_by(Answer.asked_at.desc()).limit(200)
    if not (all_users and access.is_admin(session, user)):
        stmt = stmt.where(Answer.asked_by == user.id)
    return [answer_view(a) for a in session.scalars(stmt)]


@router.get("/answers/{answer_id}", response_model=S.AnswerOut)
def get_answer(answer_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    a = session.get(Answer, answer_id)
    if a is None or (a.asked_by != user.id and not access.is_admin(session, user)):
        raise HTTPException(404, "答案不存在或无权访问")
    return answer_view(a)


@router.get("/audit", response_model=list[S.AuditOut])
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
