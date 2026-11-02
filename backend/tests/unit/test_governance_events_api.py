"""治理事件管理接口：管理员边界、一次性密钥、发件箱与失败审计可见性。"""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import JSON, MetaData, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from avelis import security
from avelis.api import app as api_app
from avelis.api.deps import current_user, get_session
from avelis.config import build_settings
from avelis.db import Base, utcnow
from avelis.events import EventType, GovernanceEvent, delivery
from avelis.models import AuditEvent, EventOutbox, User, UserRole, WebhookSubscription


@pytest.fixture
def api(monkeypatch):
    settings = build_settings({"AVELIS_SECRET": "test-secret-events-api"})
    monkeypatch.setattr(api_app, "get_settings", lambda: settings)
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    monkeypatch.setattr(delivery, "get_settings", lambda: settings)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for source in Base.metadata.sorted_tables:
        table = source.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(engine)
    app = api_app.create_app()
    with Session(engine, expire_on_commit=False) as session:
        admin = User(id="admin", username="admin", display_name="管理员", password_hash="unused")
        analyst = User(id="analyst", username="analyst", display_name="查询者", password_hash="unused")
        session.add_all(
            [admin, analyst, UserRole(user_id=admin.id, role="admin"), UserRole(user_id=analyst.id, role="analyst")]
        )
        session.commit()

        def request_session():
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

        app.dependency_overrides[get_session] = request_session
        app.dependency_overrides[current_user] = lambda: admin
        with TestClient(app) as client:
            yield SimpleNamespace(client=client, session=session, app=app, admin=admin, analyst=analyst)
    engine.dispose()


def _create(client, **changes):
    body = {
        "name": "事件接收器",
        "url": "https://93.184.216.34/hooks",
        "event_types": ["metric_version.published"],
        **changes,
    }
    response = client.post("/api/webhook-subscriptions", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _enqueue_test(client, subscription_id):
    response = client.post(f"/api/webhook-subscriptions/{subscription_id}/test")
    assert response.status_code == 202, response.text
    return response.json()


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/events/types", None),
        ("GET", "/api/webhook-subscriptions", None),
        (
            "POST",
            "/api/webhook-subscriptions",
            {"name": "hook", "url": "https://93.184.216.34/hooks", "event_types": ["metric_version.published"]},
        ),
        ("PATCH", "/api/webhook-subscriptions/missing", {"enabled": False}),
        ("DELETE", "/api/webhook-subscriptions/missing", None),
        ("POST", "/api/webhook-subscriptions/missing/rotate-secret", None),
        ("POST", "/api/webhook-subscriptions/missing/test", None),
        ("GET", "/api/event-outbox", None),
        ("POST", "/api/event-outbox/missing/redeliver", None),
    ],
)
def test_every_event_management_endpoint_requires_admin(api, method, path, body):
    api.app.dependency_overrides[current_user] = lambda: api.analyst
    response = api.client.request(method, path, json=body)
    assert response.status_code == 403, response.text
    assert list(api.session.scalars(select(AuditEvent))) == []


def test_event_types_list_matches_the_finite_contract(api):
    response = api.client.get("/api/events/types")
    assert response.status_code == 200, response.text
    assert {item["type"] for item in response.json()} == {item.value for item in EventType}


def test_subscription_crud_encrypts_and_returns_secret_only_on_create_and_rotation(api):
    subscription = _create(api.client, outcomes=["success"], event_types=["metric_version.published"] * 2)
    subscription_id = subscription["id"]
    original_secret = subscription["secret"]
    stored = api.session.get(WebhookSubscription, subscription_id)
    assert original_secret and stored.secret_enc != original_secret
    assert security.decrypt_secret(stored.secret_enc) == original_secret
    assert subscription["event_types"] == ["metric_version.published"]
    assert "secret_enc" not in subscription
    listed = api.client.get("/api/webhook-subscriptions").json()
    assert len(listed) == 1 and listed[0]["id"] == subscription_id
    assert "secret" not in listed[0] and "secret_enc" not in listed[0]
    response = api.client.patch(
        f"/api/webhook-subscriptions/{subscription_id}",
        json={"name": "新接收器", "enabled": False, "outcomes": None},
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["name"] == "新接收器" and updated["enabled"] is False and updated["outcomes"] is None
    assert "secret" not in updated and "secret_enc" not in updated
    response = api.client.post(f"/api/webhook-subscriptions/{subscription_id}/rotate-secret")
    assert response.status_code == 200, response.text
    rotated_secret = response.json()["secret"]
    assert rotated_secret != original_secret
    assert security.decrypt_secret(stored.secret_enc) == rotated_secret
    response = api.client.delete(f"/api/webhook-subscriptions/{subscription_id}")
    assert response.status_code == 204, response.text
    assert api.client.get("/api/webhook-subscriptions").json() == []
    audits = list(api.session.scalars(select(AuditEvent).where(AuditEvent.object_id == subscription_id)))
    assert {event.action for event in audits} == {
        "webhook_subscription.create",
        "webhook_subscription.update",
        "webhook_subscription.rotate_secret",
        "webhook_subscription.delete",
    }
    assert original_secret not in repr([event.detail for event in audits])
    assert rotated_secret not in repr([event.detail for event in audits])
    assert all("secret" not in event.detail and "secret_enc" not in event.detail for event in audits)


@pytest.mark.parametrize(
    "changes",
    [
        {"event_types": ["metric_version.unknown"]},
        {"event_types": []},
        {"outcomes": ["unknown"]},
        {"outcomes": []},
        {"url": "http://127.0.0.1/hook"},
        {"url": "http://10.1.2.3/hook"},
        {"url": "http://169.254.169.254/latest/meta-data"},
        {"url": "file:///tmp/hook"},
        {"secret": "client-supplied-secret"},
    ],
)
def test_invalid_subscription_is_rejected_without_audit_or_database_write(api, changes):
    response = api.client.post(
        "/api/webhook-subscriptions",
        json={
            "name": "receiver",
            "url": "https://93.184.216.34/hooks",
            "event_types": ["metric_version.published"],
            **changes,
        },
    )
    assert response.status_code == 422, response.text
    assert list(api.session.scalars(select(WebhookSubscription))) == []
    assert list(api.session.scalars(select(AuditEvent))) == []


@pytest.mark.parametrize("field", ["name", "url", "event_types", "enabled"])
def test_subscription_patch_does_not_clear_required_fields(api, field):
    subscription = _create(api.client)
    response = api.client.patch(f"/api/webhook-subscriptions/{subscription['id']}", json={field: None})
    assert response.status_code == 422, response.text
    assert api.client.get("/api/webhook-subscriptions").json()[0]["enabled"] is True


def test_test_delivery_is_queued_and_audited_without_exposing_signing_secret(api):
    subscription = _create(api.client)
    item = _enqueue_test(api.client, subscription["id"])
    assert item["subscription_id"] == subscription["id"] and item["status"] == "pending"
    assert item["attempts"] == 0 and item["event_id"] == item["payload"]["id"]
    assert set(item["payload"]) <= {"id", "type", "outcome", "at", "actor", "object", "detail", "error"}
    assert subscription["secret"] not in repr(item)
    stored = api.session.get(EventOutbox, item["id"])
    assert stored is not None
    assert stored.payload == GovernanceEvent.model_validate(item["payload"]).model_dump(mode="json", exclude_none=True)
    event = api.session.scalar(select(AuditEvent).where(AuditEvent.action == "webhook_subscription.test"))
    assert event is not None and event.detail["delivery_id"] == item["id"]


def test_outbox_filters_are_applied_before_pagination(api):
    first = _create(api.client)
    second = _create(api.client, name="另一个接收器")
    matching = [_enqueue_test(api.client, first["id"]) for _ in range(2)]
    delivered = _enqueue_test(api.client, first["id"])
    _enqueue_test(api.client, second["id"])
    api.session.get(EventOutbox, delivered["id"]).status = "delivered"
    api.session.commit()
    response = api.client.get(
        "/api/event-outbox",
        params={"subscription_id": first["id"], "status": "pending", "limit": 1, "offset": 1},
    )
    assert response.status_code == 200, response.text
    page = response.json()
    assert (page["total"], page["limit"], page["offset"], len(page["items"])) == (2, 1, 1, 1)
    assert page["items"][0]["id"] in {item["id"] for item in matching}
    assert page["items"][0]["subscription_id"] == first["id"] and page["items"][0]["status"] == "pending"


def test_only_dead_delivery_can_be_redelivered_and_retry_state_is_reset(api):
    subscription = _create(api.client)
    item = _enqueue_test(api.client, subscription["id"])
    path = f"/api/event-outbox/{item['id']}/redeliver"
    assert api.client.post(path).status_code == 409
    stored = api.session.get(EventOutbox, item["id"])
    stored.status = "dead"
    stored.attempts = 8
    stored.last_error = "receiver unavailable"
    api.session.commit()
    response = api.client.post(path)
    assert response.status_code == 200, response.text
    retried = response.json()
    assert retried["id"] == item["id"] and retried["event_id"] == item["event_id"]
    assert retried["status"] == "pending" and retried["attempts"] == 0 and retried["last_error"] is None
    event = api.session.scalar(select(AuditEvent).where(AuditEvent.action == "event_outbox.redeliver"))
    assert event is not None and event.object_id == item["id"]


def test_disabled_subscription_does_not_accept_redelivery(api):
    subscription = _create(api.client)
    item = _enqueue_test(api.client, subscription["id"])
    api.session.get(EventOutbox, item["id"]).status = "dead"
    api.session.get(WebhookSubscription, subscription["id"]).enabled = False
    api.session.commit()
    response = api.client.post(f"/api/event-outbox/{item['id']}/redeliver")
    assert response.status_code == 409, response.text
    assert api.session.get(EventOutbox, item["id"]).status == "dead"


def test_disabled_subscription_does_not_enqueue_test_delivery(api):
    subscription = _create(api.client, enabled=False)
    response = api.client.post(f"/api/webhook-subscriptions/{subscription['id']}/test")
    assert response.status_code == 409, response.text
    assert list(api.session.scalars(select(EventOutbox))) == []
    assert api.session.scalar(select(AuditEvent).where(AuditEvent.action == "webhook_subscription.test")) is None


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PATCH", "/api/webhook-subscriptions/missing"),
        ("DELETE", "/api/webhook-subscriptions/missing"),
        ("POST", "/api/webhook-subscriptions/missing/rotate-secret"),
        ("POST", "/api/webhook-subscriptions/missing/test"),
        ("POST", "/api/event-outbox/missing/redeliver"),
    ],
)
def test_missing_subscription_or_delivery_returns_404(api, method, path):
    response = api.client.request(method, path, json={} if method == "PATCH" else None)
    assert response.status_code == 404, response.text


def test_nonadmin_cannot_see_failure_audit_even_for_own_object_or_via_limit(api):
    now = utcnow()
    success = AuditEvent(
        id="success",
        at=now - timedelta(minutes=1),
        actor_id=api.analyst.id,
        actor_name=api.analyst.username,
        action="user.update",
        object_type="user",
        object_id=api.analyst.id,
        detail={},
    )
    failure = AuditEvent(
        id="failure",
        at=now,
        actor_id=api.analyst.id,
        actor_name=api.analyst.username,
        action="user.update",
        object_type="user",
        object_id=api.analyst.id,
        detail={
            "_governance": {
                "type": "metric_version.published",
                "outcome": "failure",
                "error": {"code": "WRITE_FAILED", "reason": "写操作失败"},
            }
        },
    )
    api.session.add_all([success, failure])
    api.session.commit()
    admin_response = api.client.get("/api/audit")
    assert admin_response.status_code == 200, admin_response.text
    assert {event["id"] for event in admin_response.json()} == {"success", "failure"}
    api.app.dependency_overrides[current_user] = lambda: api.analyst
    assert api.client.get("/api/audit").status_code == 403
    response = api.client.get("/api/audit", params={"object_type": "user", "object_id": api.analyst.id, "limit": 1})
    assert response.status_code == 200, response.text
    assert [event["id"] for event in response.json()] == ["success"]
    assert response.json()[0]["outcome"] == "success" and response.json()[0]["error"] is None
