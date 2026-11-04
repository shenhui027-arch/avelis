"""真实 PostgreSQL：并发槽、订阅保序、DNS 期限与第一轮健康投递。"""

import asyncio
import socket
import threading
import time
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import delete

from avelis import events
from avelis.config import build_settings
from avelis.db import new_id, session_scope, utcnow
from avelis.events import delivery
from avelis.models import EventOutbox, User, WebhookSubscription
from tests.db.test_events_concurrency import isolated_scans, world  # noqa: F401

pytestmark = pytest.mark.db


@pytest.fixture
def backlogs(world):
    created = []

    def create(counts):
        result = {}
        with session_scope() as session:
            actor = session.get(User, world[0])
            start = utcnow() - timedelta(minutes=5)
            for label, count in counts:
                subscription = WebhookSubscription(
                    id=new_id(),
                    name=label,
                    url=f"https://8.8.8.8/{label}",
                    event_types=[],
                    enabled=True,
                    secret_enc="offline-unused",
                    created_by=actor.id,
                )
                session.add(subscription)
                session.flush()
                created.append(subscription.id)
                ids = []
                for index in range(count):
                    row = events.enqueue_test(session, subscription, actor)
                    row.next_attempt_at = start + timedelta(seconds=len(result) * 10 + index)
                    ids.append(row.id)
                result[label] = subscription.id, ids
        return result

    yield create
    with session_scope() as session:
        session.execute(delete(EventOutbox).where(EventOutbox.subscription_id.in_(created)))
        session.execute(delete(WebhookSubscription).where(WebhookSubscription.id.in_(created)))


@pytest.mark.parametrize("concurrency", [1, 2, 4])
def test_postgresql_respects_concurrency_and_per_subscription_order(backlogs, monkeypatch, concurrency):
    rows = backlogs([(f"sub-{index}", 2) for index in range(6)])
    settings = build_settings(
        {"AVELIS_SECRET": "offline-v104-concurrency", "AVELIS_WEBHOOK_CONCURRENCY": str(concurrency)}
    )
    monkeypatch.setattr(delivery, "get_settings", lambda: settings)
    lock = threading.Lock()
    active, sent, maximum = set(), {}, [0]

    def send(subscription, outbox, **kwargs):
        with lock:
            assert subscription.id not in active
            active.add(subscription.id)
            maximum[0] = max(maximum[0], len(active))
            sent.setdefault(subscription.id, []).append(outbox.id)
        time.sleep(0.04)
        with lock:
            active.remove(subscription.id)

    monkeypatch.setattr(delivery, "send_webhook", send)
    assert delivery.deliver_due() == 12
    assert maximum[0] == concurrency and not active
    with session_scope() as session:
        for subscription_id, ids in rows.values():
            assert sent[subscription_id] == ids
            assert all(session.get(EventOutbox, identifier).status == "delivered" for identifier in ids)


def test_postgresql_three_unreachable_receivers_allow_healthy_delivery_in_first_round(backlogs, monkeypatch):
    rows = backlogs([(f"down-{index}", 2) for index in range(3)] + [("healthy", 1)])
    monkeypatch.setattr(delivery, "decrypt_secret", lambda encrypted: "offline-signing-secret")
    received = []

    async def receive(request):
        received.append(request.url.path)
        if request.url.path != "/healthy":
            await asyncio.sleep(10)
        return httpx.Response(204)

    started = time.monotonic()
    assert delivery.deliver_due(budget_seconds=0.4, transport=httpx.MockTransport(receive)) == 4
    assert time.monotonic() - started < 1.2 and "/healthy" in received
    with session_scope() as session:
        assert session.get(EventOutbox, rows["healthy"][1][0]).status == "delivered"
        for index in range(3):
            assert session.get(EventOutbox, rows[f"down-{index}"][1][1]).attempts == 0


def test_postgresql_blocked_dns_times_out_without_blocking_healthy_subscription(backlogs, monkeypatch):
    rows = backlogs([("dns", 1), ("healthy", 1)])
    monkeypatch.setattr(delivery, "_REQUEST_TIMEOUT_SECONDS", 0.1)
    with session_scope() as session:
        session.get(WebhookSubscription, rows["dns"][0]).url = "https://blocked-dns.example.test/hook"
    release, resolving, healthy = threading.Event(), threading.Event(), threading.Event()
    original = socket.getaddrinfo

    def resolve(hostname, *args, **kwargs):
        if hostname == "blocked-dns.example.test":
            resolving.set()
            release.wait(3)
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))]
        return original(hostname, *args, **kwargs)

    def receive(request):
        assert request.url.path == "/healthy"
        healthy.set()
        return httpx.Response(204)

    monkeypatch.setattr(delivery.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(delivery, "decrypt_secret", lambda encrypted: "offline-signing-secret")
    started = time.monotonic()
    try:
        assert delivery.deliver_due(budget_seconds=0.3, transport=httpx.MockTransport(receive)) == 2
        assert time.monotonic() - started < 1.0
        assert resolving.is_set() and healthy.is_set()
        with session_scope() as session:
            blocked = session.get(EventOutbox, rows["dns"][1][0])
            assert blocked.status == "pending" and blocked.last_error == "timeout" and blocked.attempts == 1
            assert session.get(EventOutbox, rows["healthy"][1][0]).status == "delivered"
    finally:
        release.set()


@pytest.mark.parametrize("concurrency", [1, 4])
def test_postgresql_deferred_backlog_waits_for_a_full_deadline_without_dead_lettering(
    backlogs, monkeypatch, concurrency
):
    rows = backlogs([("healthy", 2)])
    identifiers = rows["healthy"][1]
    settings = build_settings(
        {
            "AVELIS_SECRET": "offline-v104-concurrency",
            "AVELIS_WEBHOOK_CONCURRENCY": str(concurrency),
            "AVELIS_WEBHOOK_MAX_ATTEMPTS": "1",
        }
    )
    monkeypatch.setattr(delivery, "get_settings", lambda: settings)
    with session_scope() as session:
        for identifier in identifiers:
            session.get(EventOutbox, identifier).last_error = delivery.BUDGET_TIMEOUT
    clock = [100.0]
    monkeypatch.setattr(delivery, "monotonic", lambda: clock[0])
    sent = []

    def receive(_subscription, outbox, **kwargs):
        sent.append((outbox.id, kwargs["timeout_seconds"]))
        # 保留原复现的 5 秒/3 秒比例及真实 PG 认领、订阅锁与结果事务，时钟可控以免测试变慢。
        elapsed = min(3.0, kwargs["timeout_seconds"])
        clock[0] += elapsed
        if elapsed < 3.0:
            raise delivery.DeliveryError("timeout")

    monkeypatch.setattr(delivery, "send_webhook", receive)
    assert delivery.deliver_due(budget_seconds=5.0) == 1
    with session_scope() as session:
        first, second = (session.get(EventOutbox, identifier) for identifier in identifiers)
        assert first.status == "delivered" and first.attempts == 1
        assert second.status == "pending" and second.attempts == 0 and second.dead_at is None
        assert second.last_error == delivery.BUDGET_TIMEOUT
    assert delivery.deliver_due(budget_seconds=5.0) == 1
    with session_scope() as session:
        second = session.get(EventOutbox, identifiers[1])
        assert second.status == "delivered" and second.attempts == 1 and second.dead_at is None
    assert sent == [(identifier, 5.0) for identifier in identifiers]
