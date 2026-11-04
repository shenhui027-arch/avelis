"""Webhook 的网络边界、签名与逐条重试；不访问真实网络或元数据库。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import socket
import ssl
import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy.dialects import postgresql

from avelis.config import build_settings
from avelis.db import utcnow
from avelis.events import delivery
from avelis.models import EventOutbox, WebhookSubscription
from tests.unit.test_governance_events import database  # noqa: F401

PUBLIC_IP = "93.184.216.34"
SECRET = "offline-unit-test-secret-0123456789"


def dns_results(*addresses):
    return [
        (
            socket.AF_INET6 if ":" in address else socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            (address, 443),
        )
        for address in addresses
    ]


@pytest.fixture(autouse=True)
def isolated_network(monkeypatch):
    monkeypatch.setattr(delivery, "get_settings", lambda: build_settings({"AVELIS_SECRET": SECRET}))
    monkeypatch.setattr(delivery.socket, "getaddrinfo", lambda *args, **kwargs: dns_results(PUBLIC_IP))
    monkeypatch.setattr(delivery, "decrypt_secret", lambda encrypted: "signing-secret")


@pytest.fixture
def subscription():
    return WebhookSubscription(
        id="subscription-1",
        name="通知",
        url="https://hooks.example.test:8443/receive?channel=governance",
        event_types=["metric_version.published"],
        outcomes=[],
        enabled=True,
        secret_enc="encrypted-secret",
        created_by="administrator",
    )


@pytest.fixture
def outbox(subscription):
    return EventOutbox(
        id="delivery-1",
        event_id="event-1",
        subscription_id=subscription.id,
        status="pending",
        attempts=0,
        next_attempt_at=utcnow(),
        created_at=utcnow(),
        payload={
            "id": "event-1",
            "type": "metric_version.published",
            "outcome": "success",
            "at": "2026-11-03T00:00:00Z",
            "actor": {"id": "administrator", "name": "管理员"},
            "object": {"type": "metric_version", "id": "version-1"},
            "detail": {"status": "published"},
        },
    )


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "0.0.0.0",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "192.0.2.1",
        "198.18.0.1",
        "224.0.0.1",
        "240.0.0.1",
        "255.255.255.255",
        "::",
        "::1",
        "fc00::1",
        "fe80::1",
        "ff02::1",
        "2001:db8::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "2002:7f00:1::",
        "64:ff9b::7f00:1",
        "192.88.99.1",
        "3ffe::1",
        "2001:20::1",
    ],
)
def test_url_rejects_private_loopback_linklocal_reserved_and_tunnels(address):
    host = f"[{address}]" if ":" in address else address
    with pytest.raises(ValueError, match="受限地址"):
        delivery.validate_url(f"https://{host}/hook")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://public.example/hook",
        "https:///hook",
        "//public.example/hook",
        "https://user:password@public.example/hook",
        "https://user@public.example/hook",
        "https://public.example/hook#fragment",
        "https://public.example/hook#",
        "https://public.example:0/hook",
        "https://public.example:65536/hook",
        "https://[fe80::1%25eth0]/hook",
        "https://public.example\\@127.0.0.1",
        "https://public.example/\r\nHost:127.0.0.1",
        " https://public.example/hook",
        "",
    ],
)
def test_url_rejects_non_http_and_ambiguous_syntax_even_in_development(url):
    with pytest.raises(ValueError):
        delivery.validate_url(url, allow_private=True)


def test_url_checks_every_dns_address_and_does_not_accept_mixed_answers(monkeypatch):
    monkeypatch.setattr(delivery.socket, "getaddrinfo", lambda *args, **kwargs: dns_results(PUBLIC_IP, "10.0.0.1"))
    with pytest.raises(ValueError):
        delivery.validate_url("https://mixed.example/hook")
    assert delivery.validate_url("http://mixed.example/hook", allow_private=True) == [PUBLIC_IP, "10.0.0.1"]


def test_url_accepts_public_ipv4_ipv6_and_explicit_development_override(monkeypatch):
    assert delivery.validate_url("https://93.184.216.34/hook") == [PUBLIC_IP]
    ipv6 = "2606:4700:4700::1111"
    assert delivery.validate_url(f"https://[{ipv6}]/hook") == [ipv6]
    assert delivery.validate_url("http://127.0.0.1:8080/hook", allow_private=True) == ["127.0.0.1"]
    monkeypatch.setattr(
        delivery,
        "get_settings",
        lambda: build_settings({"AVELIS_SECRET": SECRET, "AVELIS_WEBHOOK_ALLOW_PRIVATE_URLS": "true"}),
    )
    assert delivery.validate_url("http://127.0.0.1/hook") == ["127.0.0.1"]


@pytest.mark.parametrize("answer", [[], socket.gaierror("sensitive DNS details")])
def test_url_dns_failure_is_safe(monkeypatch, answer):
    def resolve(*args, **kwargs):
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(delivery.socket, "getaddrinfo", resolve)
    with pytest.raises(ValueError) as caught:
        delivery.validate_url("https://missing.example/hook")
    assert "sensitive" not in str(caught.value)


def test_signed_payload_is_pinned_with_original_host_sni_and_five_second_timeout(subscription, outbox, monkeypatch):
    monkeypatch.setattr(delivery.time, "time", lambda: 1793664000)
    requests = []

    def receive(request):
        requests.append(request)
        assert request.url.host == PUBLIC_IP
        assert request.url.port == 8443
        assert request.url.raw_path == b"/receive?channel=governance"
        assert request.headers["host"] == "hooks.example.test:8443"
        assert request.extensions["sni_hostname"] == "hooks.example.test"
        timeout = request.extensions["timeout"]
        assert timeout["connect"] == 3.0
        assert 0 < timeout["read"] <= 5.0
        assert timeout["read"] == timeout["write"] == timeout["pool"]
        assert request.headers["X-Avelis-Event"] == "metric_version.published"
        assert request.headers["X-Avelis-Delivery"] == outbox.id
        timestamp = request.headers["X-Avelis-Timestamp"]
        expected = hmac.new(b"signing-secret", timestamp.encode() + b"." + request.content, hashlib.sha256).hexdigest()
        assert hmac.compare_digest(request.headers["X-Avelis-Signature"], f"sha256={expected}")
        assert json.loads(request.content) == outbox.payload
        return httpx.Response(204)

    delivery.send_webhook(subscription, outbox, transport=httpx.MockTransport(receive))
    assert len(requests) == 1


def test_delivery_does_not_follow_redirect_or_consume_response_body(subscription, outbox):
    requests = []

    class UnreadBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            pytest.fail("投递不应读取响应正文")
            yield b""  # pragma: no cover

    def receive(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"}, stream=UnreadBody())

    with pytest.raises(delivery.DeliveryError, match="^http_302$"):
        delivery.send_webhook(subscription, outbox, transport=httpx.MockTransport(receive))
    assert len(requests) == 1


def test_production_transport_connects_to_pinned_ip_and_validates_original_tls_name(subscription, outbox, monkeypatch):
    from httpcore._backends.anyio import AnyIOBackend

    connections, tls_names, written = [], [], []

    class Stream:
        async def read(self, max_bytes, timeout=None):
            return b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n"

        async def write(self, buffer, timeout=None):
            written.append(buffer)

        async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            assert ssl_context.verify_mode == ssl.CERT_REQUIRED and ssl_context.check_hostname
            tls_names.append(server_hostname)
            return self

        def get_extra_info(self, info):
            return None

        async def aclose(self):
            pass

    async def connect(self, host, port, timeout=None, local_address=None, socket_options=None):
        connections.append((host, port, timeout))
        return Stream()

    monkeypatch.setattr(AnyIOBackend, "connect_tcp", connect)
    delivery.send_webhook(subscription, outbox)
    assert connections == [(PUBLIC_IP, 8443, 3.0)]
    assert tls_names == ["hooks.example.test"]
    assert b"Host: hooks.example.test:8443\r\n" in b"".join(written)


def test_delivery_does_not_use_environment_proxy(subscription, outbox, monkeypatch):
    original = delivery.httpx.AsyncClient
    options = []

    def client(**kwargs):
        options.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(delivery.httpx, "AsyncClient", client)
    delivery.send_webhook(subscription, outbox, transport=httpx.MockTransport(lambda request: httpx.Response(204)))
    assert options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False


def test_dns_rebinding_cannot_change_the_validated_connection(subscription, outbox, monkeypatch):
    lookups = []

    def resolve(*args, **kwargs):
        lookups.append(args[0])
        return dns_results(PUBLIC_IP if len(lookups) == 1 else "127.0.0.1")

    def receive(request):
        assert request.url.host == PUBLIC_IP
        assert len(lookups) == 1
        return httpx.Response(204)

    monkeypatch.setattr(delivery.socket, "getaddrinfo", resolve)
    delivery.send_webhook(subscription, outbox, transport=httpx.MockTransport(receive))
    with pytest.raises(delivery.DeliveryError, match="url_rejected"):
        delivery.send_webhook(subscription, outbox, transport=httpx.MockTransport(receive))
    assert len(lookups) == 2


@pytest.mark.parametrize("failure", [httpx.ConnectError("unreachable"), httpx.ConnectTimeout("unreachable")])
def test_connection_failure_falls_back_to_next_validated_ip_with_remaining_budget(
    subscription, outbox, monkeypatch, failure
):
    first, second = "2606:4700:4700::1111", PUBLIC_IP
    monkeypatch.setattr(delivery.socket, "getaddrinfo", lambda *args, **kwargs: dns_results(first, second))
    ticks = iter([100.0, 100.0, 100.0, 100.0, 103.0])
    monkeypatch.setattr(delivery, "monotonic", lambda: next(ticks))
    requests = []

    def receive(request):
        requests.append(request)
        if request.url.host == first:
            raise failure
        assert request.url.host == second
        return httpx.Response(204)

    delivery.send_webhook(subscription, outbox, transport=httpx.MockTransport(receive))
    assert len(requests) == 2
    assert requests[0].extensions["timeout"] == {"connect": 3.0, "read": 5.0, "write": 5.0, "pool": 5.0}
    assert requests[1].extensions["timeout"] == dict.fromkeys(("connect", "read", "write", "pool"), 2.0)
    assert requests[0].content == requests[1].content
    for header in ("Host", "X-Avelis-Event", "X-Avelis-Delivery", "X-Avelis-Signature", "X-Avelis-Timestamp"):
        assert requests[0].headers[header] == requests[1].headers[header]
    assert requests[0].extensions["sni_hostname"] == requests[1].extensions["sni_hostname"] == "hooks.example.test"


def test_connection_fallback_stops_when_shared_budget_is_exhausted(subscription, outbox, monkeypatch):
    monkeypatch.setattr(delivery.socket, "getaddrinfo", lambda *args, **kwargs: dns_results(PUBLIC_IP, "8.8.8.8"))
    ticks = iter([100.0, 100.0, 100.0, 100.0, 105.0])
    monkeypatch.setattr(delivery, "monotonic", lambda: next(ticks))
    requests = []

    def receive(request):
        requests.append(request)
        raise httpx.ConnectTimeout("unreachable")

    with pytest.raises(delivery.DeliveryError, match="^timeout$"):
        delivery.send_webhook(subscription, outbox, transport=httpx.MockTransport(receive))
    assert len(requests) == 1


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("sent"), httpx.WriteError("partially sent"), 503])
def test_connection_fallback_does_not_repeat_requests_that_may_have_been_sent(
    subscription, outbox, monkeypatch, failure
):
    monkeypatch.setattr(delivery.socket, "getaddrinfo", lambda *args, **kwargs: dns_results(PUBLIC_IP, "8.8.8.8"))
    requests = []

    def receive(request):
        requests.append(request)
        if isinstance(failure, Exception):
            raise failure
        return httpx.Response(failure)

    with pytest.raises(delivery.DeliveryError):
        delivery.send_webhook(subscription, outbox, transport=httpx.MockTransport(receive))
    assert len(requests) == 1


@pytest.mark.parametrize("status", [400, 401, 429, 500])
def test_http_failure_stores_only_status_code(subscription, outbox, status):
    transport = httpx.MockTransport(lambda request: httpx.Response(status, text="password=do-not-store"))
    with pytest.raises(delivery.DeliveryError) as caught:
        delivery.send_webhook(subscription, outbox, transport=transport)
    assert str(caught.value) == f"http_{status}"


@pytest.mark.parametrize(
    ("exception", "code"),
    [
        (httpx.ReadTimeout("secret=do-not-store"), "timeout"),
        (httpx.ConnectError("password=do-not-store"), "network_error"),
    ],
)
def test_network_failure_does_not_store_exception_text(subscription, outbox, exception, code):
    def fail(request):
        raise exception

    with pytest.raises(delivery.DeliveryError) as caught:
        delivery.send_webhook(subscription, outbox, transport=httpx.MockTransport(fail))
    assert str(caught.value) == code


def test_payload_must_follow_the_contract(subscription, outbox):
    outbox.payload = {**outbox.payload, "password": "must-not-leave"}
    with pytest.raises(delivery.DeliveryError, match="invalid_payload"):
        delivery.send_webhook(
            subscription, outbox, transport=httpx.MockTransport(lambda request: pytest.fail("不能投递"))
        )


@pytest.fixture
def delivery_database(database, monkeypatch):
    with database() as session:
        engine = session.get_bind()
    monkeypatch.setattr(delivery, "get_engine", lambda: engine)
    return database


def queue_subscription_backlogs(database, outbox, backlogs):
    start = utcnow() - timedelta(minutes=5)
    position = 0
    with database() as session:
        for identifier, count in backlogs:
            session.add(
                WebhookSubscription(
                    id=identifier,
                    name=identifier,
                    url=f"https://{identifier}.example.test/hook",
                    secret_enc="unused",
                    event_types=["metric_version.published"],
                    enabled=True,
                )
            )
            for index in range(count):
                event_id = f"{identifier}-{index}"
                session.add(
                    EventOutbox(
                        id=event_id,
                        event_id=event_id,
                        subscription_id=identifier,
                        payload={**outbox.payload, "id": event_id},
                        status="pending",
                        attempts=0,
                        next_attempt_at=start + timedelta(seconds=position),
                        created_at=start + timedelta(seconds=position),
                    )
                )
                position += 1


def test_due_delivery_success_uses_independent_short_transactions(delivery_database, monkeypatch, outbox):
    queue_subscription_backlogs(delivery_database, outbox, [("first", 1)])
    claimed = []
    original = delivery._claim_due

    def claim(*args):
        result = original(*args)
        if result:
            claimed.append(result)
        return result

    def receive(request):
        assert not claimed[0].connection.in_transaction()
        return httpx.Response(204)

    monkeypatch.setattr(delivery, "_claim_due", claim)
    assert delivery.deliver_due(transport=httpx.MockTransport(receive)) == 1
    with delivery_database() as session:
        row = session.get(EventOutbox, "first-0")
        assert row.status == "delivered" and row.attempts == 1
        assert row.delivered_at is not None and row.last_error is None and row.dead_at is None
    assert claimed[0].connection.closed


@pytest.mark.parametrize(("previous", "delay"), [(0, 1), (1, 2), (2, 4), (6, 60)])
def test_due_delivery_exponential_retry_is_capped(delivery_database, monkeypatch, outbox, previous, delay):
    now = utcnow()
    monkeypatch.setattr(delivery, "utcnow", lambda: now)
    queue_subscription_backlogs(delivery_database, outbox, [("first", 1)])
    with delivery_database() as session:
        session.get(EventOutbox, "first-0").attempts = previous
    assert delivery.deliver_due(transport=httpx.MockTransport(lambda request: httpx.Response(503))) == 1
    with delivery_database() as session:
        row = session.get(EventOutbox, "first-0")
        assert row.status == "pending" and row.attempts == previous + 1
        assert row.next_attempt_at.replace(tzinfo=None) == now + timedelta(minutes=delay)
        assert row.last_error == "http_503"


def test_configured_retry_limit_sets_dead_at_without_stopping_other_entries(delivery_database, monkeypatch, outbox):
    settings = build_settings({"AVELIS_SECRET": SECRET, "AVELIS_WEBHOOK_MAX_ATTEMPTS": "3"})
    monkeypatch.setattr(delivery, "get_settings", lambda: settings)
    queue_subscription_backlogs(delivery_database, outbox, [("first", 1), ("second", 1)])
    with delivery_database() as session:
        session.get(EventOutbox, "first-0").attempts = 2
    assert delivery.deliver_due(transport=httpx.MockTransport(lambda request: httpx.Response(500))) == 2
    with delivery_database() as session:
        first, second = session.get(EventOutbox, "first-0"), session.get(EventOutbox, "second-0")
        assert first.status == "dead" and first.attempts == 3 and first.dead_at is not None
        assert second.status == "pending" and second.attempts == 1 and second.dead_at is None


def test_transaction_failure_is_logged_without_details_and_other_subscription_runs(
    delivery_database, monkeypatch, outbox, caplog
):
    queue_subscription_backlogs(delivery_database, outbox, [("first", 1), ("second", 1)])
    original = delivery._finish_claim

    def finish(claim, *args):
        if claim.subscription.id == "first":
            raise RuntimeError("database-password=do-not-log")
        return original(claim, *args)

    monkeypatch.setattr(delivery, "_finish_claim", finish)
    assert delivery.deliver_due(transport=httpx.MockTransport(lambda request: httpx.Response(204))) == 2
    with delivery_database() as session:
        assert session.get(EventOutbox, "second-0").status == "delivered"
    assert "transaction failed" in caplog.text and "do-not-log" not in caplog.text


def test_due_claim_locks_rows_and_excludes_disabled_not_due_terminal_and_already_handled():
    statement = delivery._due_statement(utcnow(), {"claimed-id"}, {"blocked-subscription"})
    sql = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "FOR NO KEY UPDATE SKIP LOCKED" in sql
    assert statement._for_update_arg.key_share is True
    assert statement._for_update_arg.read is False
    assert statement._for_update_arg.skip_locked is True
    assert "JOIN webhook_subscription" in sql
    assert "webhook_subscription.enabled IS true" in sql
    assert "event_outbox.status = 'pending'" in sql
    assert "event_outbox.next_attempt_at <= " in sql
    assert "event_outbox.id NOT IN ('claimed-id')" in sql
    assert "event_outbox.subscription_id NOT IN ('blocked-subscription')" in sql
    assert "LIMIT 1" in sql


@pytest.mark.parametrize("concurrency", [1, 2, 4])
def test_concurrency_limit_and_subscription_order(delivery_database, monkeypatch, outbox, concurrency):
    settings = build_settings({"AVELIS_SECRET": SECRET, "AVELIS_WEBHOOK_CONCURRENCY": str(concurrency)})
    monkeypatch.setattr(delivery, "get_settings", lambda: settings)
    queue_subscription_backlogs(delivery_database, outbox, [(f"sub-{index}", 3) for index in range(6)])
    lock = threading.Lock()
    in_flight, maximum, order = set(), [0], {}

    def send(subscription, item, **kwargs):
        with lock:
            assert subscription.id not in in_flight
            in_flight.add(subscription.id)
            maximum[0] = max(maximum[0], len(in_flight))
            order.setdefault(subscription.id, []).append(item.id)
        time.sleep(0.03)
        with lock:
            in_flight.remove(subscription.id)

    monkeypatch.setattr(delivery, "send_webhook", send)
    assert delivery.deliver_due() == 18
    assert maximum[0] == concurrency and not in_flight
    for identifier, sent in order.items():
        assert sent == [f"{identifier}-{index}" for index in range(3)]


@pytest.mark.parametrize("batch_size", [2, 3, 50])
def test_due_subscriptions_rotate_before_processing_next_event(delivery_database, monkeypatch, outbox, batch_size):
    queue_subscription_backlogs(delivery_database, outbox, [("first", 3), ("second", 2), ("third", 2)])
    claimed = []
    original = delivery._claim_due

    def claim(*args):
        result = original(*args)
        if result is not None:
            claimed.append(result.outbox.id)
        return result

    monkeypatch.setattr(delivery, "_claim_due", claim)
    monkeypatch.setattr(delivery, "send_webhook", lambda *args, **kwargs: time.sleep(0.01))
    assert delivery.deliver_due(batch_size=batch_size) == min(batch_size, 7)
    assert claimed[: min(batch_size, 3)] == ["first-0", "second-0", "third-0"][:batch_size]


@pytest.mark.parametrize(("budget_seconds", "batch_size"), [(12, 50), (5, 50), (5, 1)])
def test_every_request_uses_full_remaining_deadline_without_fair_share(
    delivery_database, monkeypatch, outbox, budget_seconds, batch_size
):
    monkeypatch.setattr(delivery, "monotonic", lambda: 100.0)
    queue_subscription_backlogs(delivery_database, outbox, [("first", 1), ("second", 1)])
    timeouts = []
    monkeypatch.setattr(
        delivery, "send_webhook", lambda _subscription, _item, **kwargs: timeouts.append(kwargs["timeout_seconds"])
    )
    assert delivery.deliver_due(batch_size=batch_size, budget_seconds=budget_seconds) == min(batch_size, 2)
    assert timeouts == [5.0] * min(batch_size, 2)


def test_multiple_unreachable_subscriptions_do_not_block_healthy_first_round(delivery_database, outbox):
    queue_subscription_backlogs(
        delivery_database, outbox, [(f"down-{index}", 2) for index in range(3)] + [("healthy", 1)]
    )
    received = []

    async def receive(request):
        received.append(request.headers["Host"])
        if request.headers["Host"] != "healthy.example.test":
            await asyncio.sleep(10)
        return httpx.Response(204)

    started = time.monotonic()
    assert delivery.deliver_due(budget_seconds=0.2, transport=httpx.MockTransport(receive)) == 4
    assert time.monotonic() - started < 0.8
    assert "healthy.example.test" in received
    with delivery_database() as session:
        assert session.get(EventOutbox, "healthy-0").status == "delivered"
        for index in range(3):
            untouched = session.get(EventOutbox, f"down-{index}-1")
            assert untouched.status == "pending" and untouched.attempts == 0


def test_dns_deadline_does_not_block_another_subscription(delivery_database, monkeypatch, outbox):
    queue_subscription_backlogs(delivery_database, outbox, [("blocked-dns", 1), ("healthy", 1)])
    release, blocked, healthy = threading.Event(), threading.Event(), threading.Event()

    def resolve(host, *args, **kwargs):
        if host == "blocked-dns.example.test":
            blocked.set()
            release.wait(2)
        return dns_results(PUBLIC_IP)

    def receive(request):
        assert request.headers["Host"] == "healthy.example.test"
        healthy.set()
        return httpx.Response(204)

    monkeypatch.setattr(delivery.socket, "getaddrinfo", resolve)
    started = time.monotonic()
    try:
        assert delivery.deliver_due(budget_seconds=0.15, transport=httpx.MockTransport(receive)) == 2
        assert blocked.is_set() and healthy.is_set()
        assert time.monotonic() - started < 0.6
        with delivery_database() as session:
            blocked_row = session.get(EventOutbox, "blocked-dns-0")
            assert blocked_row.status == "pending" and blocked_row.last_error == delivery.BUDGET_TIMEOUT
            assert session.get(EventOutbox, "healthy-0").status == "delivered"
    finally:
        release.set()


def test_expired_claim_does_not_send_or_mutate_record(delivery_database, monkeypatch, outbox):
    queue_subscription_backlogs(delivery_database, outbox, [("first", 1)])
    clock = [100.0]
    monkeypatch.setattr(delivery, "monotonic", lambda: clock[0])
    original = delivery._claim_due

    def claim(*args):
        result = original(*args)
        clock[0] = 105.0
        return result

    monkeypatch.setattr(delivery, "_claim_due", claim)
    monkeypatch.setattr(delivery, "send_webhook", lambda *args, **kwargs: pytest.fail("预算耗尽后不能发送"))
    assert delivery.deliver_due() == 0
    with delivery_database() as session:
        row = session.get(EventOutbox, "first-0")
        assert row.status == "pending" and row.attempts == 0 and row.last_error is None


def test_pool_wait_is_bounded_and_late_claim_is_released(monkeypatch):
    release, closed = threading.Event(), threading.Event()
    late = SimpleNamespace(connection=SimpleNamespace(close=closed.set))

    def claim():
        release.wait(2)
        return late

    def release_claim(claim):
        claim.connection.close()
        closed.set()

    monkeypatch.setattr(delivery, "_release_claim", release_claim)
    started = time.monotonic()
    try:
        assert asyncio.run(delivery._claim_before_deadline(claim, delivery.monotonic() + 0.05)) is None
        assert time.monotonic() - started < 0.4
    finally:
        release.set()
    assert closed.wait(1), "迟到认领必须解锁和归还连接"


def test_claim_wait_shortens_first_wave_without_consuming_healthy_retries(delivery_database, monkeypatch, outbox):
    queue_subscription_backlogs(delivery_database, outbox, [("healthy", 1)])
    clock = [100.0]
    monkeypatch.setattr(delivery, "monotonic", lambda: clock[0])
    original = delivery._claim_due

    def claim(*args):
        result = original(*args)
        if result:
            clock[0] += 3.0
        return result

    def send(_subscription, _outbox, **kwargs):
        assert kwargs["timeout_seconds"] == 2.0
        raise delivery.DeliveryError("timeout")

    monkeypatch.setattr(delivery, "_claim_due", claim)
    monkeypatch.setattr(delivery, "send_webhook", send)
    assert delivery.deliver_due() == 1
    with delivery_database() as session:
        row = session.get(EventOutbox, "healthy-0")
        assert row.status == "pending" and row.attempts == 0 and row.last_error == delivery.BUDGET_TIMEOUT
    monkeypatch.setattr(delivery, "_claim_due", original)
    monkeypatch.setattr(delivery, "send_webhook", lambda *args, **kwargs: None)
    assert delivery.deliver_due() == 1
    with delivery_database() as session:
        assert session.get(EventOutbox, "healthy-0").status == "delivered"


def test_blocked_dns_is_shared_and_resolver_threads_have_a_hard_limit(monkeypatch):
    release, started = threading.Event(), threading.Event()
    calls = []

    def resolve(*args, **kwargs):
        calls.append(True)
        started.set()
        release.wait(2)
        return dns_results(PUBLIC_IP)

    monkeypatch.setattr(delivery.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(delivery, "_DNS_SLOTS", threading.BoundedSemaphore(1))
    try:
        for _ in range(2):
            with pytest.raises(TimeoutError):
                delivery._resolve_before_deadline("https://stuck.example.test/hook", delivery.monotonic() + 0.02)
        assert started.is_set() and len(calls) == 1
        with pytest.raises(TimeoutError):
            delivery._resolve_before_deadline("https://another.example.test/hook", delivery.monotonic() + 0.02)
        assert len(calls) == 1
    finally:
        release.set()


def test_timeout_budget_preserves_health_and_deferred_records_precede_new_records(
    delivery_database, monkeypatch, outbox
):
    queue_subscription_backlogs(
        delivery_database, outbox, [(f"down-{index}", 1) for index in range(10)] + [("healthy", 1)]
    )
    sent = []

    def send(subscription, item, **kwargs):
        sent.append(item.id)
        if subscription.id != "healthy":
            time.sleep(0.015)
            raise delivery.DeliveryError("timeout")
        if kwargs["timeout_seconds"] < 0.04:
            raise delivery.DeliveryError("timeout")
        time.sleep(0.04)

    monkeypatch.setattr(delivery, "send_webhook", send)
    for _ in range(10):
        delivery.deliver_due(budget_seconds=0.07)
        with delivery_database() as session:
            if session.get(EventOutbox, "healthy-0").status == "delivered":
                break
    with delivery_database() as session:
        healthy = session.get(EventOutbox, "healthy-0")
        assert healthy.status == "delivered" and healthy.attempts == 1
        for index in range(10):
            assert session.get(EventOutbox, f"down-{index}-0").status != "dead"

    queue_subscription_backlogs(delivery_database, outbox, [("deferred", 1), ("new", 1)])
    with delivery_database() as session:
        session.get(EventOutbox, "deferred-0").last_error = delivery.BUDGET_TIMEOUT
        session.get(EventOutbox, "new-0").next_attempt_at = utcnow() - timedelta(days=1)
    sent.clear()
    monkeypatch.setattr(delivery, "send_webhook", lambda _subscription, item, **kwargs: sent.append(item.id))
    assert delivery.deliver_due(batch_size=1) == 1
    assert sent == ["deferred-0"]


def test_success_resets_consecutive_failure_count(delivery_database, monkeypatch, outbox):
    queue_subscription_backlogs(delivery_database, outbox, [("first", 5)])
    statuses = iter([500, 204, 500, 500])
    assert delivery.deliver_due(transport=httpx.MockTransport(lambda request: httpx.Response(next(statuses)))) == 4
    with delivery_database() as session:
        assert session.get(EventOutbox, "first-4").attempts == 0


def test_disabled_future_and_terminal_entries_are_not_attempted(delivery_database, outbox):
    queue_subscription_backlogs(
        delivery_database, outbox, [("disabled", 1), ("future", 1), ("dead", 1), ("delivered", 1)]
    )
    with delivery_database() as session:
        session.get(WebhookSubscription, "disabled").enabled = False
        session.get(EventOutbox, "future-0").next_attempt_at = utcnow() + timedelta(days=1)
        session.get(EventOutbox, "dead-0").status = "dead"
        session.get(EventOutbox, "delivered-0").status = "delivered"
    assert delivery.deliver_due(transport=httpx.MockTransport(lambda request: pytest.fail("不能发送"))) == 0
