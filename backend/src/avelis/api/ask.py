"""问数、历史答案与依据、审计。"""

from __future__ import annotations

import asyncio
import contextvars
import queue
import threading
from collections.abc import AsyncIterator
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from avelis import audit, charts, usage
from avelis.api import schemas as S
from avelis.api.deps import current_user, get_session
from avelis.api.sse import KEEPALIVE_FRAME, format_sse, sse_response
from avelis.ask import agent
from avelis.ask.insights import build as build_insights
from avelis.ask.intent import Intent
from avelis.ask.service import _result_contract, filter_alignment_restricted, project_alignment_clarifications
from avelis.db import utcnow
from avelis.governance import access
from avelis.governance.errors import GovernanceError
from avelis.i18n import translate
from avelis.models import Answer, AskThread, AuditEvent, Metric, MetricVersion, User

router = APIRouter(prefix="/api", tags=["问数与审计"])


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    thread_id: str | None = Field(default=None, description="继续已有会话（含回答追问）；为空则新建会话")


def _answer_result(a: Answer, session: Session | None) -> dict:
    result = _answer_contract(a, session)
    if a.status == "answered" and isinstance(result, dict) and "column_roles" in result:
        return {
            **result,
            "insights": build_insights(
                result.get("columns") or [],
                result.get("rows") or [],
                result["column_roles"],
                result.get("units") or {},
                result.get("polarity") or {},
                time_aggregations=result.get("time_aggregations"),
                truncated=result.get("truncated", False),
            ),
            "charts": charts.recommend(
                result.get("columns") or [],
                result.get("rows") or [],
                result["column_roles"],
                result.get("units") or {},
                result.get("polarity") or {},
                getattr(a, "intent", None),
                preference=(getattr(a, "evidence", None) or {}).get("chart"),
            ),
        }
    if a.status == "answered" and isinstance(result, dict) and result.get("truncated"):
        return {**result, "insights": []}
    return result


def _answer_contract(a: Answer, session: Session | None) -> dict:
    """旧答案只按已锁定版本补展示字段，不改持久化结果，不查询最新口径。"""
    result = a.result
    if session is None or a.status != "answered" or not isinstance(result, dict):
        return result
    columns = result.get("columns")
    if not isinstance(columns, list) or not columns or any(not isinstance(column, str) for column in columns):
        return result
    roles = result.get("column_roles")
    if roles is not None and (
        "time_aggregations" in result
        or not any(roles.get(column, {}).get("role") == "time" for column in columns)
        or not charts.selected_measures(columns, roles)
    ):
        return result
    try:
        intent = Intent.model_validate(a.intent)
    except ValidationError:
        return result  # 旧意图无法按当前契约解释时，保留原记录。
    if not intent.metric_codes:
        return result
    versions = {}
    ambiguous = set()
    for version_id in dict.fromkeys(getattr(a, "metric_version_ids", None) or []):
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
    projected = _result_contract(result, intent, requested, value_column="value" if simple else None)
    if roles is not None:
        return {**result, "time_aggregations": projected["time_aggregations"]}
    return projected


def answer_view(a: Answer, session: Session | None = None, *, user: User | None = None) -> dict:
    evidence = a.evidence or {}
    alignments = evidence.get("filter_alignment") or []
    historical_alignments = [entry for step in a.steps or [] for entry in step.get("filter_alignment") or []]
    evidence, candidates_restricted = project_alignment_clarifications(session, user, evidence)
    _, historical_candidates_restricted = project_alignment_clarifications(session, user, a.steps or [])
    candidates_restricted |= historical_candidates_restricted
    restricted = filter_alignment_restricted(session, user, [*alignments, *historical_alignments])
    restricted |= candidates_restricted
    if restricted:
        evidence = {
            key: value for key, value in evidence.items() if key not in {"filter_alignment", "filter_alignment_unknown"}
        }
    return {
        "id": a.id,
        "question": a.question,
        "asked_by": a.asked_by,
        "asked_at": a.asked_at.isoformat(),
        "status": a.status,
        "message": None if candidates_restricted else a.message,
        "error_code": a.error_code,
        "model_adapter": a.model_adapter,
        "intent": a.intent,
        "plan": a.plan,
        "sql": a.sql,
        "metric_version_ids": a.metric_version_ids,
        "result": _answer_result(a, session),
        "evidence": evidence,
        "filter_alignment": [] if restricted else alignments,
        "freshness": a.freshness,
        "executed_at": a.executed_at.isoformat() if a.executed_at else None,
        "duration_ms": a.duration_ms,
        "cache_hit": a.cache_hit,
        "recheck_status": a.recheck_status,
        "thread_id": a.thread_id,
        "steps": []
        if candidates_restricted
        else [
            {key: value for key, value in step.items() if key not in {"filter_alignment", "filter_alignment_unknown"}}
            for step in a.steps
        ]
        if restricted
        else a.steps,
        "explanation": None if restricted else a.explanation,
        "explanation_source": None if restricted else a.explanation_source,
        "feedback_rating": getattr(a, "feedback_rating", None),
        "feedback_note": getattr(a, "feedback_note", None),
        "feedback_at": a.feedback_at.isoformat() if getattr(a, "feedback_at", None) else None,
    }


def _stream_ask(body: Question, request: Request, user: User, session: Session):
    from sqlalchemy.orm import sessionmaker

    from avelis import db

    out: queue.Queue[tuple[str, dict | None]] = queue.Queue()
    user_id = user.id
    bind = session.get_bind() if isinstance(session, Session) else None
    worker_factory = (
        sessionmaker(bind=bind, expire_on_commit=False, future=True)
        if bind is not None
        else (
            db.session_factory() if not hasattr(session, "_is_coroutine") and hasattr(db, "session_factory") else None
        )
    )

    def emit(event: str, payload: dict) -> None:
        out.put((event, payload))

    def worker() -> None:
        # SSE 在独立线程内使用独立数据库 Session，避免与请求线程 teardown 并发碰撞
        own_session = isinstance(session, Session) and bind is not None
        worker_session = worker_factory() if own_session and worker_factory is not None else session
        try:
            worker_user = (
                worker_session.get(User, user_id, populate_existing=True) if own_session and user_id else user
            ) or user
            answer = agent.run_turn(worker_session, worker_user, body.question, body.thread_id, on_event=emit)
            view = answer_view(answer, worker_session, user=worker_user)
            if hasattr(worker_session, "commit"):
                worker_session.commit()
            out.put(("done", view))
        except GovernanceError as exc:
            if hasattr(worker_session, "rollback"):
                worker_session.rollback()
            out.put(("error", {"code": "governance_error", "message": translate(exc.message), "detail": exc.detail}))
        except Exception as exc:  # noqa: BLE001
            if hasattr(worker_session, "rollback"):
                worker_session.rollback()
            out.put(("error", {"code": "ask_error", "message": type(exc).__name__}))
        finally:
            if own_session and hasattr(worker_session, "close"):
                worker_session.close()
            out.put(("__end__", None))

    if isinstance(session, Session) and hasattr(session, "close"):
        session.close()

    thread = threading.Thread(target=contextvars.copy_context().run, args=(worker,), daemon=True)
    thread.start()

    async def event_stream() -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        seq = 0
        idle_ticks = 0
        while True:
            if await request.is_disconnected():
                break
            try:
                event, payload = await loop.run_in_executor(None, out.get, True, 1.0)
                idle_ticks = 0
            except queue.Empty:
                idle_ticks += 1
                if idle_ticks >= 15:
                    idle_ticks = 0
                    yield KEEPALIVE_FRAME
                continue
            if event == "__end__":
                break
            seq += 1
            yield format_sse(event, payload, event_id=seq)

    return sse_response(event_stream())


@router.post("/ask", response_model=S.AnswerOut)
def ask(
    body: Question,
    request: Request,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    usage.enforce_llm_preflight(request, session, user, action="ask")
    if "text/event-stream" in (request.headers.get("accept") or "").lower():
        return _stream_ask(body, request, user, session)
    return answer_view(agent.run_turn(session, user, body.question, body.thread_id), session, user=user)


@router.post(
    "/ask/stream",
    responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}},
)
def ask_stream(
    body: Question,
    request: Request,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    usage.enforce_llm_preflight(request, session, user, action="ask")
    return _stream_ask(body, request, user, session)


@router.get("/ask/threads", response_model=list[S.ThreadOut])
def list_threads(user: User = Depends(current_user), session: Session = Depends(get_session)) -> list[dict]:
    return [
        {
            "id": t.id,
            "title": t.title,
            "status": t.status,
            "created_at": t.created_at.isoformat(),
            "updated_at": t.updated_at.isoformat(),
        }
        for t in session.scalars(
            select(AskThread).where(AskThread.user_id == user.id).order_by(AskThread.updated_at.desc()).limit(100)
        )
    ]


@router.get("/ask/threads/{thread_id}", response_model=S.ThreadDetailOut)
def get_thread(thread_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    t = session.get(AskThread, thread_id)
    if t is None or t.user_id != user.id:
        raise HTTPException(404, "会话不存在")
    answers = session.scalars(select(Answer).where(Answer.thread_id == t.id).order_by(Answer.asked_at))
    compacted_summary, compacted_at = agent.thread_compaction_info(session, t.id)
    return {
        "id": t.id,
        "title": t.title,
        "status": t.status,
        "answers": [answer_view(a, session, user=user) for a in answers],
        "compacted_summary": compacted_summary,
        "compacted_at": compacted_at,
    }


@router.post("/ask/threads/{thread_id}/compact", response_model=S.ThreadCompactOut)
def compact_thread(thread_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    result = agent.compact_thread(session, user, thread_id)
    session.commit()
    return result


@router.get("/answers", response_model=list[S.AnswerOut])
def list_answers(
    all_users: bool = False, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> list[dict]:
    stmt = select(Answer).order_by(Answer.asked_at.desc()).limit(200)
    if not (all_users and access.is_admin(session, user)):
        stmt = stmt.where(Answer.asked_by == user.id)
    return [answer_view(a, session, user=user) for a in session.scalars(stmt)]


@router.get("/answers/{answer_id}", response_model=S.AnswerOut)
def get_answer(answer_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    a = session.get(Answer, answer_id)
    if a is None or (a.asked_by != user.id and not access.is_admin(session, user)):
        raise HTTPException(404, "答案不存在或无权访问")
    if a.status == "answered":
        audit.record(
            session,
            user,
            "answer.view",
            "answer",
            a.id,
            asked_by=a.asked_by,
            row_count=len((a.result or {}).get("rows") or []),
        )
        session.commit()
    return answer_view(a, session, user=user)


@router.post("/answers/{answer_id}/feedback", response_model=S.AnswerOut)
def answer_feedback(
    answer_id: str,
    body: S.AnswerFeedbackIn,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    a = session.get(Answer, answer_id)
    if a is None or a.asked_by != user.id:
        raise HTTPException(404, "答案不存在或无权访问")
    if a.status == "clarify":
        raise HTTPException(409, "追问中间状态不支持反馈")
    if body.rating is None:
        a.feedback_rating = None
        a.feedback_note = None
        a.feedback_at = None
        a.feedback_by = None
    else:
        a.feedback_rating = body.rating
        a.feedback_note = (body.note or "").strip() or None
        a.feedback_at = utcnow()
        a.feedback_by = user.id
    audit.record(session, user, "answer.feedback", "answer", a.id, rating=a.feedback_rating)
    session.commit()
    return answer_view(a, session, user=user)


@router.get("/audit", response_model=list[S.AuditOut])
def list_audit(
    object_type: str | None = None,
    object_id: str | None = None,
    action: str | None = None,
    actor_name: str | None = None,
    request_id: str | None = None,
    trace_id: str | None = None,
    client_ip: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    # 全量审计只给管理员；按对象查看历史时要求能看到该对象
    admin = access.is_admin(session, user)
    if not admin:
        if not (object_type and object_id):
            raise HTTPException(403, "只有管理员可以查看全部审计")
        if not access.can_see_object(session, user, object_type, object_id):
            raise HTTPException(404, "对象不存在或无权访问")
    stmt = select(AuditEvent).order_by(AuditEvent.at.desc())
    if not admin:
        stmt = stmt.where(
            func.coalesce(AuditEvent.detail["_governance"]["outcome"].as_string(), "success") != "failure"
        )
    if object_type:
        stmt = stmt.where(AuditEvent.object_type == object_type)
    if object_id:
        stmt = stmt.where(AuditEvent.object_id == object_id)
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    if actor_name:
        stmt = stmt.where(AuditEvent.actor_name == actor_name)
    if request_id:
        stmt = stmt.where(AuditEvent.detail["request_id"].as_string() == request_id)
    if trace_id:
        stmt = stmt.where(AuditEvent.detail["trace_id"].as_string() == trace_id)
    if client_ip:
        stmt = stmt.where(AuditEvent.detail["client_ip"].as_string() == client_ip)
    if since:
        stmt = stmt.where(AuditEvent.at >= since)
    if until:
        stmt = stmt.where(AuditEvent.at < until)
    stmt = stmt.offset(offset).limit(min(limit, 1000))
    return [
        {
            "id": e.id,
            "at": e.at.isoformat(),
            "actor_id": e.actor_id,
            "actor": e.actor_name,
            "action": e.action,
            "object_type": e.object_type,
            "object_id": e.object_id,
            "detail": e.detail,
            "client_ip": (e.detail or {}).get("client_ip"),
            "user_agent": (e.detail or {}).get("user_agent"),
            "request_id": (e.detail or {}).get("request_id"),
            "trace_id": (e.detail or {}).get("trace_id"),
            "outcome": (e.detail.get("_governance") or {}).get("outcome", "success"),
            "event_type": (e.detail.get("_governance") or {}).get("type"),
            "error": (e.detail.get("_governance") or {}).get("error"),
        }
        for e in session.scalars(stmt)
    ]
