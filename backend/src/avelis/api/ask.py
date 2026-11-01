"""问数、历史答案与依据、审计。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis.api import schemas as S
from avelis.api.deps import current_user, get_session
from avelis.ask import agent
from avelis.ask.intent import Intent
from avelis.ask.service import _result_contract
from avelis.governance import access
from avelis.models import Answer, AskThread, AuditEvent, Metric, MetricVersion, User

router = APIRouter(prefix="/api", tags=["问数与审计"])


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    thread_id: str | None = Field(default=None, description="继续已有会话（含回答追问）；为空则新建会话")


def _answer_result(a: Answer, session: Session | None) -> dict:
    """旧答案只按已锁定版本补展示字段，不改持久化结果，不查询最新口径。"""
    result = a.result
    if session is None or a.status != "answered" or not isinstance(result, dict) or "column_roles" in result:
        return result
    columns = result.get("columns")
    if not isinstance(columns, list) or not columns or any(not isinstance(column, str) for column in columns):
        return result
    try:
        intent = Intent.model_validate(a.intent)
    except ValidationError:
        return result  # 旧意图无法按当前契约解释时，保留原记录。
    if not intent.metric_codes:
        return result
    versions = {}
    ambiguous = set()
    for version_id in dict.fromkeys(a.metric_version_ids or []):
        version = session.get(MetricVersion, version_id)
        metric = session.get(Metric, version.metric_id) if version else None
        if metric is None or metric.code not in {*intent.metric_codes, intent.metric_code}:
            continue
        if metric.code in versions and versions[metric.code][1].id != version.id:
            ambiguous.add(metric.code)
        versions[metric.code] = (metric, version)
    primary = versions.get(intent.metric_code)
    if intent.is_simple and primary is None:
        return result
    simple = intent.is_simple and primary is not None and primary[1].kind != "derived"
    codes = [intent.metric_code] if simple else intent.metric_codes
    if any(code not in versions or code in ambiguous for code in codes):
        return result  # 所问指标缺少锁定版本或锁了多个版本，不能猜。
    requested = [versions[code] for code in codes]
    if simple and "value" not in columns:
        return result
    if not simple and any(metric.name not in columns for metric, _ in requested):
        return result  # 指标可能已改名，无法把当前显示名安全地映射回历史列。
    return _result_contract(result, intent, requested, value_column="value" if simple else None)


def answer_view(a: Answer, session: Session | None = None) -> dict:
    return {"id": a.id, "question": a.question, "asked_by": a.asked_by, "asked_at": a.asked_at.isoformat(),
            "status": a.status, "message": a.message, "error_code": a.error_code, "model_adapter": a.model_adapter,
            "intent": a.intent, "plan": a.plan, "sql": a.sql, "metric_version_ids": a.metric_version_ids,
            "result": _answer_result(a, session), "evidence": a.evidence, "freshness": a.freshness,
            "executed_at": a.executed_at.isoformat() if a.executed_at else None, "duration_ms": a.duration_ms,
            "cache_hit": a.cache_hit, "recheck_status": a.recheck_status, "thread_id": a.thread_id,
            "steps": a.steps, "explanation": a.explanation, "explanation_source": a.explanation_source}


@router.post("/ask", response_model=S.AnswerOut)
def ask(body: Question, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    return answer_view(agent.run_turn(session, user, body.question, body.thread_id), session)


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
    return {"id": t.id, "title": t.title, "status": t.status, "answers": [answer_view(a, session) for a in answers]}


@router.get("/answers", response_model=list[S.AnswerOut])
def list_answers(all_users: bool = False, user: User = Depends(current_user),
                 session: Session = Depends(get_session)) -> list[dict]:
    stmt = select(Answer).order_by(Answer.asked_at.desc()).limit(200)
    if not (all_users and access.is_admin(session, user)):
        stmt = stmt.where(Answer.asked_by == user.id)
    return [answer_view(a, session) for a in session.scalars(stmt)]


@router.get("/answers/{answer_id}", response_model=S.AnswerOut)
def get_answer(answer_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    a = session.get(Answer, answer_id)
    if a is None or (a.asked_by != user.id and not access.is_admin(session, user)):
        raise HTTPException(404, "答案不存在或无权访问")
    return answer_view(a, session)


@router.get("/audit", response_model=list[S.AuditOut])
def list_audit(object_type: str | None = None, object_id: str | None = None, action: str | None = None,
               limit: int = 200, user: User = Depends(current_user), session: Session = Depends(get_session)):
    # 全量审计只给管理员；按对象查看历史时要求能看到该对象
    if not access.is_admin(session, user):
        if not (object_type and object_id):
            raise HTTPException(403, "只有管理员可以查看全部审计")
        if not access.can_see_object(session, user, object_type, object_id):
            raise HTTPException(404, "对象不存在或无权访问")
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
