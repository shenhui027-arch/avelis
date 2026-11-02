"""治理事件契约、审计投影与独立失败留底（ADR 0018）。"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import event as sa_event
from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis.db import new_id, session_scope, utcnow
from avelis.models import AuditEvent, EventOutbox, User, WebhookSubscription

log = logging.getLogger(__name__)


class GovernanceEventType(StrEnum):
    METRIC_VERSION_SUBMITTED = "metric_version.submitted"
    METRIC_VERSION_PUBLISHED = "metric_version.published"
    METRIC_VERSION_REJECTED = "metric_version.rejected"
    METRIC_VERSION_AFFECTED = "metric_version.affected"
    METRIC_VERSION_INVALIDATED = "metric_version.invalidated"
    JOIN_PATH_APPROVED = "join_path.approved"
    JOIN_PATH_AFFECTED = "join_path.affected"
    COLLECT_COMPLETED = "collect.completed"
    COLLECT_FAILED = "collect.failed"
    COLLECT_SCHEMA_CHANGED = "collect.schema_changed"
    LINEAGE_IMPORT_COMPLETED = "lineage_import.completed"
    LINEAGE_IMPORT_FAILED = "lineage_import.failed"
    ATTRIBUTION_UNAVAILABLE = "attribution.unavailable"
    AUTH_LOGIN_FAILURE_THRESHOLD = "auth.login_failure_threshold"
    WEBHOOK_TEST = "webhook.test"


EventType = GovernanceEventType
EVENT_TYPES = tuple(EventType)
SUBSCRIBABLE_EVENT_TYPES = tuple(t for t in EventType if t != EventType.WEBHOOK_TEST)
# 保留客户端 EventType 的可订阅契约；完整事件类型使用 GovernanceEventType。
SubscribableEventType = StrEnum("EventType", {t.name: t.value for t in SUBSCRIBABLE_EVENT_TYPES})
# 仅显式映射的 action 才产生外部事件；枚举值也用作失败留底的 action。
ACTION_EVENT_TYPES: dict[str, EventType] = {
    **{t.value: t for t in SUBSCRIBABLE_EVENT_TYPES},
    "metric_version.submit": EventType.METRIC_VERSION_SUBMITTED,
    "metric_version.approve": EventType.METRIC_VERSION_PUBLISHED,
    "metric_version.reject": EventType.METRIC_VERSION_REJECTED,
    "join_path.decide": EventType.JOIN_PATH_APPROVED,
    "join_path.reapproval_required": EventType.JOIN_PATH_AFFECTED,
    "collect": EventType.COLLECT_COMPLETED,
    "import.completed": EventType.LINEAGE_IMPORT_COMPLETED,
    "import.failed": EventType.LINEAGE_IMPORT_FAILED,
}
FAILURE_TYPES = frozenset(
    (EventType.COLLECT_FAILED, EventType.LINEAGE_IMPORT_FAILED, EventType.AUTH_LOGIN_FAILURE_THRESHOLD)
)


class EventActor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str


class EventObject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    id: str | None
    name: str | None = None


class EventError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    reason: str


class GovernanceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    type: EventType
    outcome: Literal["success", "failure"]
    at: datetime
    actor: EventActor | Literal["system"]
    object: EventObject
    detail: dict[str, Any] = Field(default_factory=dict)
    error: EventError | None = None

    @field_validator("at")
    @classmethod
    def utc_at(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def failure_has_error(self) -> GovernanceEvent:
        if self.outcome == "failure" and self.error is None:
            raise ValueError("failure events require an error")
        return self


# 审计仍保留正常结构字段；机密键、SQL 原文与数据行即使嵌套也不允许写入。
_PRIVATE_KEY = re.compile(
    r"password|passwd|passphrase|credential|secret|token|authorization|api[_-]?key|"
    r"^(?:sql|sql_text|statement|statement_text|query|query_text|rows?|records?|raw_text|expression)$",
    re.I,
)
# URL 固定第一个冒号，避免两个可匹配冒号的量词重叠；/ 和空白隔开各起点。
_PRIVATE_TEXT = re.compile(
    r"\b(?:insert\s+into|delete\s+from|"
    r"(?:create|alter|drop)\s+(?:table|view)|bearer\s+\S+|"
    r"(?:password|passwd|secret|token|api[_-]?key)\s*[:=])|://[^/\s:]*:[^/\s]*@",
    re.I,
)
_PRIVATE_SQL = (
    (re.compile(r"\bselect\s", re.I), re.compile(r"\sfrom", re.I)),
    (re.compile(r"\bupdate\s", re.I), re.compile(r"\sset", re.I)),
)
_MAX_TEXT_SCAN_CHARS = 64 * 1024


def _contains_private_text(value: str) -> bool:
    if _PRIVATE_TEXT.search(value):
        return True
    for start, end in _PRIVATE_SQL:
        first = start.search(value)
        # 最早起点已覆盖所有后续配对；+1 保留原 .+ 至少一个字符的语义。
        if first is not None and end.search(value, first.end() + 1):
            return True
    return False


def sanitize_detail(detail: dict[str, Any]) -> dict[str, Any]:
    def clean(value: Any, depth: int = 0) -> Any:
        if depth > 12:
            return None
        if isinstance(value, dict):
            return {
                str(k): clean(v, depth + 1)
                for k, v in value.items()
                if k != "_governance"
                and (not _PRIVATE_KEY.search(str(k)) or (str(k) in {"rows", "records"} and type(v) is int and v >= 0))
            }
        if isinstance(value, (list, tuple)):
            return [clean(v, depth + 1) for v in value[:200]]
        if isinstance(value, str):
            if len(value) > _MAX_TEXT_SCAN_CHARS or _contains_private_text(value):
                return "[redacted]"
            return value[:2000]
        if value is None or isinstance(value, (int, float, bool)):
            return value
        return None

    return clean(detail)


# 外部投影是允许列表：不把自由文本 note/reason/warnings、解析器输出或源端报错传给订阅。
_EXTERNAL_FIELDS = frozenset(
    (
        "run_id",
        "batch_id",
        "job_id",
        "trigger",
        "dependency_fingerprint",
        "change_set_id",
        "status",
        "decision",
        "domain_id",
        "cause",
        "failures",
        "threshold",
        "test",
        "event_count",
        "change_count",
        "files",
        "succeeded",
        "failed",
        "needs_review",
        "changes",
        "statements",
        "edges",
        "review_task_id",
        "replacement_version_id",
        "resolution",
    )
)


def prepare_audit(row: AuditEvent) -> None:
    event_type = ACTION_EVENT_TYPES.get(row.action)
    if event_type is None:
        return
    if row.action == "join_path.decide" and not (
        row.detail.get("status") == "approved" and row.detail.get("decision") == "approved"
    ):
        return
    outcome = "failure" if event_type in FAILURE_TYPES else "success"
    metadata: dict[str, Any] = {"type": event_type.value, "outcome": outcome}
    if outcome == "failure":
        metadata["error"] = {"code": event_type.name, "reason": "操作失败"}
    row.detail = {**row.detail, "_governance": metadata}


def event_from_audit(row: AuditEvent) -> GovernanceEvent | None:
    metadata = (row.detail or {}).get("_governance")
    if not metadata:
        return None
    detail = sanitize_detail({k: v for k, v in row.detail.items() if k in _EXTERNAL_FIELDS})
    return GovernanceEvent(
        id=row.id,
        type=metadata["type"],
        outcome=metadata["outcome"],
        at=row.at.replace(tzinfo=timezone.utc) if row.at.tzinfo is None else row.at.astimezone(timezone.utc),
        actor=EventActor(id=row.actor_id, name=row.actor_name) if row.actor_id else "system",
        object=EventObject(type=row.object_type, id=row.object_id),
        detail=detail,
        error=metadata.get("error"),
    )


def event_payload(event: GovernanceEvent) -> dict[str, Any]:
    payload = event.model_dump(mode="json", exclude_none=True)
    payload["object"].setdefault("id", None)
    return payload


def enqueue_event(session: Session, subscription: WebhookSubscription, event: GovernanceEvent) -> EventOutbox:
    row = EventOutbox(
        id=new_id(),
        event_id=event.id,
        subscription_id=subscription.id,
        payload=event_payload(event),
        status="pending",
        attempts=0,
        next_attempt_at=utcnow(),
        created_at=utcnow(),
    )
    session.add(row)
    return row


@sa_event.listens_for(Session, "before_flush")
def _audit_outbox(session: Session, _flush_context, _instances) -> None:
    audits = [row for row in session.new if isinstance(row, AuditEvent) and (row.detail or {}).get("_governance")]
    if not audits:
        return
    subscriptions = [
        subscription
        for subscription in session.scalars(
            select(WebhookSubscription)
            .where(WebhookSubscription.enabled.is_(True))
            .order_by(WebhookSubscription.id)
            .with_for_update(read=True, key_share=True)
        )
        if subscription.enabled and subscription not in session.deleted
    ]
    for subscription in session.new:
        if isinstance(subscription, WebhookSubscription) and subscription.enabled is not False:
            if subscription.id is None:
                subscription.id = new_id()
            subscriptions.append(subscription)
    for row in audits:
        event = event_from_audit(row)
        for subscription in subscriptions:
            if event.type in subscription.event_types and (
                not subscription.outcomes or event.outcome in subscription.outcomes
            ):
                enqueue_event(session, subscription, event)


def enqueue_test(session: Session, subscription: WebhookSubscription, actor: User) -> EventOutbox:
    return enqueue_event(
        session,
        subscription,
        GovernanceEvent(
            id=new_id(),
            type=EventType.WEBHOOK_TEST,
            outcome="success",
            at=datetime.now(timezone.utc),
            actor=EventActor(id=actor.id, name=actor.username),
            object=EventObject(type="webhook_subscription", id=subscription.id, name=subscription.name),
            detail={"test": True},
        ),
    )


def record_failure(
    session: Session,
    actor: User | str | None,
    event_type: EventType | str,
    object_type: str,
    object_id: str | None = None,
    *,
    error_code: str = "OPERATION_FAILED",
    error_reason: str = "操作失败",
    **detail: Any,
) -> AuditEvent:
    """在调用方事务中记录失败；对象改为 failed 时必须同事务调用，不接受异常原文作为外部原因。"""
    from avelis import audit

    if isinstance(actor, str):
        actor = session.get(User, actor)
    kind = EventType(event_type)
    row = audit.record(session, actor, kind.value, object_type, object_id, **detail)
    # reason 仅保留预定义短原因；任意数据库、解析器或 HTTP 异常的原文都可能含敏感内容。
    safe_reasons = {"操作失败", "后台任务运行超时", "登录失败次数超过阈值", "测试事件"}
    code = error_code if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", error_code) else "OPERATION_FAILED"
    row.detail = {
        **row.detail,
        "_governance": {
            "type": kind.value,
            "outcome": "failure",
            "error": {
                "code": code,
                "reason": error_reason if error_reason in safe_reasons else "操作失败",
            },
        },
    }
    return row


@contextmanager
def failure_guard(
    event_type: EventType | str,
    actor: User | str | None,
    object_type: str,
    object_id: str | None = None,
    *,
    session: Session | None = None,
    **detail: Any,
) -> Iterator[None]:
    """只为未记录事件的异常独立兜底；谁把对象改为 failed，谁在同一事务写失败事件。"""
    actor_id = actor.id if isinstance(actor, User) else actor
    actor_name = actor.username if isinstance(actor, User) else None
    try:
        yield
    except Exception as original:
        if getattr(original, "failure_event_recorded", False) is True:
            raise
        try:
            if session is not None:
                session.rollback()
            with session_scope() as failure_session:
                row = record_failure(
                    failure_session,
                    actor_id,
                    event_type,
                    object_type,
                    object_id,
                    error_code=type(original).__name__,
                    **detail,
                )
                if actor_id and actor_name:
                    row.actor_id, row.actor_name = actor_id, actor_name
        except Exception:
            # 不输出异常原文或 traceback：失败可能来自带 SQL/连接串的数据库异常。
            log.error("治理失败事件未能写入；原业务异常将继续抛出")
        raise
