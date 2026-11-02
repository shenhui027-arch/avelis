"""Webhook 的网络边界、签名与逐条重试；不访问真实网络或元数据库。"""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
import ssl
from contextlib import contextmanager
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


def test_signed_payload_is_pinned_with_original_host_sni_and_ten_second_timeout(subscription, outbox, monkeypatch):
    monkeypatch.setattr(delivery.time, "time", lambda: 1793664000)
    requests = []

    def receive(request):
        requests.append(request)
        assert request.url.host == PUBLIC_IP
        assert request.url.port == 8443
        assert request.url.raw_path == b"/receive?channel=governance"
        assert request.headers["host"] == "hooks.example.test:8443"
        assert request.extensions["sni_hostname"] == "hooks.example.test"
        assert request.extensions["timeout"] == dict.fromkeys(("connect", "read", "write", "pool"), 10.0)
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

    class UnreadBody(httpx.SyncByteStream):
        def __iter__(self):
            pytest.fail("投递不应读取响应正文")

    def receive(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"}, stream=UnreadBody())

    with pytest.raises(delivery.DeliveryError, match="^http_302$"):
        delivery.send_webhook(subscription, outbox, transport=httpx.MockTransport(receive))
    assert len(requests) == 1


def test_production_transport_connects_to_pinned_ip_and_validates_original_tls_name(subscription, outbox, monkeypatch):
    from httpcore._backends.sync import SyncBackend

    connections, tls_names, written = [], [], []

    class Stream:
        def read(self, max_bytes, timeout=None):
            return b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n"

        def write(self, buffer, timeout=None):
            written.append(buffer)

        def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            assert ssl_context.verify_mode == ssl.CERT_REQUIRED and ssl_context.check_hostname
            tls_names.append(server_hostname)
            return self

        def get_extra_info(self, info):
            return None

        def close(self):
            pass

    def connect(self, host, port, timeout=None, local_address=None, socket_options=None):
        connections.append((host, port, timeout))
        return Stream()

    monkeypatch.setattr(SyncBackend, "connect_tcp", connect)
    delivery.send_webhook(subscription, outbox)
    assert connections == [(PUBLIC_IP, 8443, 10.0)]
    assert tls_names == ["hooks.example.test"]
    assert b"Host: hooks.example.test:8443\r\n" in b"".join(written)


def test_delivery_does_not_use_environment_proxy(subscription, outbox, monkeypatch):
    original = delivery.httpx.Client
    options = []

    def client(**kwargs):
        options.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(delivery.httpx, "Client", client)
    delivery.send_webhook(subscription, outbox, transport=httpx.MockTransport(lambda request: httpx.Response(204)))
    assert options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False
    assert options[0]["timeout"] == 10.0


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
    ticks = iter([100.0, 103.0])
    monkeypatch.setattr(delivery.time, "monotonic", lambda: next(ticks))
    requests = []

    def receive(request):
        requests.append(request)
        if request.url.host == first:
            raise failure
        assert request.url.host == second
        return httpx.Response(204)

    delivery.send_webhook(subscription, outbox, transport=httpx.MockTransport(receive))
    assert len(requests) == 2
    assert requests[0].extensions["timeout"] == dict.fromkeys(("connect", "read", "write", "pool"), 10.0)
    assert requests[1].extensions["timeout"] == dict.fromkeys(("connect", "read", "write", "pool"), 7.0)
    assert requests[0].content == requests[1].content
    for header in ("Host", "X-Avelis-Event", "X-Avelis-Delivery", "X-Avelis-Signature", "X-Avelis-Timestamp"):
        assert requests[0].headers[header] == requests[1].headers[header]
    assert requests[0].extensions["sni_hostname"] == requests[1].extensions["sni_hostname"] == "hooks.example.test"


def test_connection_fallback_stops_when_shared_budget_is_exhausted(subscription, outbox, monkeypatch):
    monkeypatch.setattr(delivery.socket, "getaddrinfo", lambda *args, **kwargs: dns_results(PUBLIC_IP, "8.8.8.8"))
    ticks = iter([100.0, 110.0])
    monkeypatch.setattr(delivery.time, "monotonic", lambda: next(ticks))
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


def fake_transactions(monkeypatch, rows, *, fail_commit_once=False):
    transactions = []
    selected = iter(rows)

    @contextmanager
    def scope():
        transactions.append("begin")
        try:
            yield SimpleNamespace(execute=lambda statement: SimpleNamespace(first=lambda: next(selected, None)))
            if fail_commit_once and len(transactions) == 1:
                raise RuntimeError("database-password=do-not-log")
            transactions.append("commit")
        except Exception:
            transactions.append("rollback")
            raise

    monkeypatch.setattr(delivery, "session_scope", scope)
    return transactions


def test_due_delivery_success_commits_independently(monkeypatch, subscription, outbox):
    transactions = fake_transactions(monkeypatch, [(outbox, subscription)])
    assert delivery.deliver_due(transport=httpx.MockTransport(lambda request: httpx.Response(204))) == 1
    assert outbox.status == "delivered" and outbox.attempts == 1
    assert outbox.delivered_at is not None and outbox.last_error is None
    assert transactions == ["begin", "commit", "begin", "commit"]


@pytest.mark.parametrize(("previous", "delay"), [(0, 1), (1, 2), (2, 4), (6, 60)])
def test_due_delivery_exponential_retry_is_capped(monkeypatch, subscription, outbox, previous, delay):
    now = utcnow()
    outbox.attempts = previous
    monkeypatch.setattr(delivery, "utcnow", lambda: now)
    fake_transactions(monkeypatch, [(outbox, subscription)])
    assert delivery.deliver_due(transport=httpx.MockTransport(lambda request: httpx.Response(503))) == 1
    assert outbox.status == "pending" and outbox.attempts == previous + 1
    assert outbox.next_attempt_at == now + timedelta(minutes=delay)
    assert outbox.last_error == "http_503"


def test_due_delivery_marks_dead_and_does_not_stop_next_entry(monkeypatch, subscription, outbox):
    outbox.attempts = 7
    other = EventOutbox(
        id="delivery-2",
        event_id="event-1",
        subscription_id=subscription.id,
        payload=outbox.payload,
        status="pending",
        attempts=0,
    )
    fake_transactions(monkeypatch, [(outbox, subscription), (other, subscription)])
    assert delivery.deliver_due(transport=httpx.MockTransport(lambda request: httpx.Response(500))) == 2
    assert outbox.status == "dead" and outbox.attempts == 8
    assert other.status == "pending" and other.attempts == 1


def test_configured_retry_limit_and_cap_are_used(monkeypatch, subscription, outbox):
    settings = build_settings(
        {"AVELIS_SECRET": SECRET, "AVELIS_WEBHOOK_MAX_ATTEMPTS": "3", "AVELIS_WEBHOOK_RETRY_MAX_MINUTES": "1"}
    )
    monkeypatch.setattr(delivery, "get_settings", lambda: settings)
    outbox.attempts = 2
    fake_transactions(monkeypatch, [(outbox, subscription)])
    delivery.deliver_due(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    assert outbox.status == "dead" and outbox.attempts == 3


def test_transaction_failure_is_logged_without_details_and_other_entry_runs(monkeypatch, subscription, outbox, caplog):
    other = EventOutbox(
        id="delivery-2",
        event_id="event-1",
        subscription_id=subscription.id,
        payload=outbox.payload,
        status="pending",
        attempts=0,
    )
    transactions = fake_transactions(
        monkeypatch, [(outbox, subscription), (other, subscription)], fail_commit_once=True
    )
    assert delivery.deliver_due(transport=httpx.MockTransport(lambda request: httpx.Response(204))) == 2
    assert transactions[:3] == ["begin", "rollback", "begin"]
    assert other.status == "delivered"
    assert "transaction failed" in caplog.text and "do-not-log" not in caplog.text


def test_due_claim_locks_rows_and_excludes_disabled_not_due_terminal_and_already_handled():
    statement = delivery._due_statement(utcnow(), {"claimed-id"})
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
    assert "LIMIT 1" in sql


def test_persisted_disabled_future_and_terminal_entries_are_not_attempted(database, monkeypatch, subscription, outbox):
    monkeypatch.setattr(delivery, "session_scope", database)
    disabled = WebhookSubscription(
        id="disabled-subscription",
        name="disabled",
        url=subscription.url,
        event_types=subscription.event_types,
        outcomes=[],
        enabled=False,
        secret_enc="encrypted-secret",
    )
    with database() as session:
        session.add_all([subscription, disabled, outbox])
        for identifier, status, sub_id, next_attempt in (
            ("disabled", "pending", disabled.id, utcnow()),
            ("future", "pending", subscription.id, utcnow() + timedelta(days=1)),
            ("dead", "dead", subscription.id, utcnow()),
            ("delivered", "delivered", subscription.id, utcnow()),
        ):
            session.add(
                EventOutbox(
                    id=identifier,
                    event_id=identifier,
                    subscription_id=sub_id,
                    payload={**outbox.payload, "id": identifier},
                    status=status,
                    attempts=0,
                    next_attempt_at=next_attempt,
                )
            )

    received = []

    def receive(request):
        received.append(request.headers["X-Avelis-Delivery"])
        return httpx.Response(204)

    transport = httpx.MockTransport(receive)
    assert delivery.deliver_due(transport=transport) == 1
    assert received == [outbox.id]
    with database() as session:
        for identifier in ("disabled", "future", "dead", "delivered"):
            row = session.get(EventOutbox, identifier)
            assert row.attempts == 0
        assert session.get(EventOutbox, "disabled").status == "pending"
        session.get(WebhookSubscription, disabled.id).enabled = True
    assert delivery.deliver_due(transport=transport) == 1
    assert received == [outbox.id, "disabled"]
    with database() as session:
        assert session.get(EventOutbox, "disabled").status == "delivered"
