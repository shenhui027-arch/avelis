"""治理事件与 Webhook 管理：配置与发件箱只对管理员开放。"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from avelis import audit, events
from avelis.api.deps import current_user, get_session
from avelis.db import utcnow
from avelis.events import GovernanceEvent, SubscribableEventType
from avelis.events.delivery import validate_url
from avelis.governance import access
from avelis.models import EventOutbox, User, WebhookSubscription
from avelis.security import encrypt_secret

router = APIRouter(prefix="/api", tags=["治理事件与 Webhook"])

Outcome = Literal["success", "failure"]
OutboxStatus = Literal["pending", "delivered", "dead"]


class EventTypeOut(BaseModel):
    type: SubscribableEventType


class SubscriptionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1, max_length=2048)
    event_types: list[SubscribableEventType] = Field(min_length=1, max_length=100)
    outcomes: list[Outcome] | None = Field(default=None, min_length=1, max_length=2)
    enabled: bool = True

    @field_validator("name", "url")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class SubscriptionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    event_types: list[SubscribableEventType] | None = Field(default=None, min_length=1, max_length=100)
    outcomes: list[Outcome] | None = Field(default=None, min_length=1, max_length=2)
    enabled: bool | None = None

    @field_validator("name", "url")
    @classmethod
    def nonblank(cls, value: str | None) -> str | None:
        return SubscriptionCreate.nonblank(value) if value is not None else None


class SubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    url: str
    event_types: list[SubscribableEventType]
    outcomes: list[Outcome] | None
    enabled: bool
    created_by: str | None
    created_at: datetime
    updated_at: datetime


class SubscriptionSecretOut(SubscriptionOut):
    secret: str = Field(description="签名密钥，仅创建或轮换时返回一次")


class OutboxOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    event_id: str
    subscription_id: str
    payload: GovernanceEvent
    status: OutboxStatus
    attempts: int
    next_attempt_at: datetime
    last_error: str | None
    created_at: datetime
    delivered_at: datetime | None


class OutboxPage(BaseModel):
    items: list[OutboxOut]
    total: int
    limit: int
    offset: int


def _validate_url(url: str) -> None:
    try:
        validate_url(url)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


def _subscription(session: Session, subscription_id: str) -> WebhookSubscription:
    subscription = session.scalar(
        select(WebhookSubscription).where(WebhookSubscription.id == subscription_id).with_for_update()
    )
    if subscription is None:
        raise HTTPException(404, "Webhook 订阅不存在")
    return subscription


def _secret_view(subscription: WebhookSubscription, secret: str) -> dict:
    return {**SubscriptionOut.model_validate(subscription).model_dump(), "secret": secret}


@router.get("/events/types", response_model=list[EventTypeOut])
def list_event_types(user: User = Depends(current_user), session: Session = Depends(get_session)) -> list[dict]:
    access.require_role(session, user, "admin")
    return [{"type": item} for item in events.SUBSCRIBABLE_EVENT_TYPES]


@router.get("/webhook-subscriptions", response_model=list[SubscriptionOut])
def list_subscriptions(
    user: User = Depends(current_user), session: Session = Depends(get_session)
) -> list[WebhookSubscription]:
    access.require_role(session, user, "admin")
    return list(session.scalars(select(WebhookSubscription).order_by(WebhookSubscription.created_at.desc())))


@router.post("/webhook-subscriptions", response_model=SubscriptionSecretOut, status_code=201)
def create_subscription(
    body: SubscriptionCreate, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    access.require_role(session, user, "admin")
    _validate_url(body.url)
    secret = secrets.token_urlsafe(32)
    subscription = WebhookSubscription(
        name=body.name,
        url=body.url,
        event_types=list(dict.fromkeys(body.event_types)),
        outcomes=list(dict.fromkeys(body.outcomes)) if body.outcomes else None,
        enabled=body.enabled,
        secret_enc=encrypt_secret(secret),
        created_by=user.id,
    )
    session.add(subscription)
    session.flush()
    audit.record(session, user, "webhook_subscription.create", "webhook_subscription", subscription.id)
    return _secret_view(subscription, secret)


@router.patch("/webhook-subscriptions/{subscription_id}", response_model=SubscriptionOut)
def update_subscription(
    subscription_id: str,
    body: SubscriptionUpdate,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> WebhookSubscription:
    access.require_role(session, user, "admin")
    changes = body.model_dump(exclude_unset=True)
    if any(value is None for key, value in changes.items() if key != "outcomes"):
        raise HTTPException(422, "订阅字段不能为空")
    subscription = _subscription(session, subscription_id)
    if "url" in changes:
        _validate_url(changes["url"])
    for key in ("event_types", "outcomes"):
        if changes.get(key) is not None:
            changes[key] = list(dict.fromkeys(changes[key]))
    for key, value in changes.items():
        setattr(subscription, key, value)
    subscription.updated_at = utcnow()
    audit.record(
        session, user, "webhook_subscription.update", "webhook_subscription", subscription.id, fields=sorted(changes)
    )
    session.flush()
    return subscription


@router.delete("/webhook-subscriptions/{subscription_id}", status_code=204)
def delete_subscription(
    subscription_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> None:
    access.require_role(session, user, "admin")
    subscription = _subscription(session, subscription_id)
    session.execute(delete(EventOutbox).where(EventOutbox.subscription_id == subscription_id))
    session.delete(subscription)
    audit.record(session, user, "webhook_subscription.delete", "webhook_subscription", subscription_id)


@router.post("/webhook-subscriptions/{subscription_id}/rotate-secret", response_model=SubscriptionSecretOut)
def rotate_secret(
    subscription_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    access.require_role(session, user, "admin")
    subscription = _subscription(session, subscription_id)
    secret = secrets.token_urlsafe(32)
    subscription.secret_enc = encrypt_secret(secret)
    subscription.updated_at = utcnow()
    audit.record(session, user, "webhook_subscription.rotate_secret", "webhook_subscription", subscription_id)
    session.flush()
    return _secret_view(subscription, secret)


@router.post("/webhook-subscriptions/{subscription_id}/test", response_model=OutboxOut, status_code=202)
def test_subscription(
    subscription_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> EventOutbox:
    access.require_role(session, user, "admin")
    subscription = _subscription(session, subscription_id)
    if not subscription.enabled:
        raise HTTPException(409, "Webhook 订阅已停用")
    _validate_url(subscription.url)
    item = events.enqueue_test(session, subscription, user)
    session.flush()
    audit.record(
        session, user, "webhook_subscription.test", "webhook_subscription", subscription_id, delivery_id=item.id
    )
    session.flush()
    return item


@router.get("/event-outbox", response_model=OutboxPage)
def list_outbox(
    subscription_id: str | None = None,
    status: OutboxStatus | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    access.require_role(session, user, "admin")
    conditions = []
    if subscription_id is not None:
        conditions.append(EventOutbox.subscription_id == subscription_id)
    if status is not None:
        conditions.append(EventOutbox.status == status)
    total = session.scalar(select(func.count()).select_from(EventOutbox).where(*conditions))
    items = session.scalars(
        select(EventOutbox)
        .where(*conditions)
        .order_by(EventOutbox.created_at.desc(), EventOutbox.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return {"items": list(items), "total": total, "limit": limit, "offset": offset}


@router.post("/event-outbox/{delivery_id}/redeliver", response_model=OutboxOut)
def redeliver(
    delivery_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> EventOutbox:
    access.require_role(session, user, "admin")
    subscription_id = session.scalar(select(EventOutbox.subscription_id).where(EventOutbox.id == delivery_id))
    if subscription_id is None:
        raise HTTPException(404, "发件箱条目不存在")
    # 与删除订阅保持相同锁序，避免订阅锁和发件箱锁互相等待。
    subscription = _subscription(session, subscription_id)
    item = session.scalar(select(EventOutbox).where(EventOutbox.id == delivery_id).with_for_update())
    if item is None:
        raise HTTPException(404, "发件箱条目不存在")
    if item.status != "dead":
        raise HTTPException(409, "只有投递终止的条目可以重投")
    if not subscription.enabled:
        raise HTTPException(409, "Webhook 订阅已停用")
    _validate_url(subscription.url)
    item.status = "pending"
    item.attempts = 0
    item.next_attempt_at = utcnow()
    item.last_error = None
    item.delivered_at = None
    audit.record(session, user, "event_outbox.redeliver", "event_outbox", item.id)
    session.flush()
    return item
