"""Webhook 发件箱投递；网络与数据库失败不影响已经提交的业务审计。"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from time import monotonic
from urllib.parse import urlsplit

import httpx
from sqlalchemy import case, or_, select, text
from sqlalchemy.orm import Session

from avelis.config import get_settings
from avelis.db import get_engine, utcnow
from avelis.events import GovernanceEvent, event_payload
from avelis.models import EventOutbox, WebhookSubscription
from avelis.security import decrypt_secret

logger = logging.getLogger(__name__)
_REQUEST_TIMEOUT_SECONDS = 5.0
_DNS_SLOTS = threading.BoundedSemaphore(64)
_CLAIM_SLOTS = threading.BoundedSemaphore(64)
_DELIVERY_SLOTS = threading.BoundedSemaphore(64)
_DNS_LOCK = threading.Lock()
_DNS_IN_FLIGHT: dict[str, concurrent.futures.Future] = {}
# 只有整轮剩余期限不足时的超时才推迟，不再按订阅数切分请求期限。
BUDGET_TIMEOUT = "timeout_budget"
_RESTRICTED_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in ("192.88.99.0/24", "3ffe::/16", "2001:10::/28", "2001:20::/28", "64:ff9b::/96")
)


class DeliveryError(Exception):
    """只保存固定错误码；不保存目标响应或可能含凭据的客户端异常。"""


def _public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    # 补充 Python 3.12 地址表中仍可能标为 global 的已废弃转发网段和不可路由的 ORCHID。
    if any(ip in network for network in _RESTRICTED_NETWORKS if network.version == ip.version):
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        # IPv4 兼容/转换隧道可能把全局 IPv6 地址转到私网 IPv4；不接收这些地址。
        if ip.ipv4_mapped is not None:
            return _public_address(str(ip.ipv4_mapped))
        if ip.sixtofour is not None or ip.teredo is not None:
            return False
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def validate_url(url: str, *, allow_private: bool | None = None) -> list[str]:
    """验证所有解析结果并返回可连接的 IP；投递时必须重新验证且钉住该结果。"""
    if allow_private is None:
        allow_private = get_settings().webhook_allow_private_urls
    try:
        if not url or any(ord(char) <= 32 or ord(char) == 127 for char in url) or "\\" in url:
            raise ValueError
        parsed = urlsplit(url)
        target = httpx.URL(url)
        if (
            parsed.scheme.lower() not in ("http", "https")
            or not target.host
            or parsed.username is not None
            or parsed.password is not None
            or "#" in url
            or "%" in target.host
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
        ):
            raise ValueError
        hostname = target.raw_host.decode("ascii")
        try:
            addresses = [str(ipaddress.ip_address(hostname))]
        except ValueError:
            results = socket.getaddrinfo(
                hostname,
                target.port or (443 if target.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
            addresses = list(dict.fromkeys(str(ipaddress.ip_address(item[4][0])) for item in results))
        if not addresses or (not allow_private and any(not _public_address(address) for address in addresses)):
            raise ValueError
        return addresses
    except (ValueError, OSError, httpx.InvalidURL, UnicodeError):
        raise ValueError("Webhook URL 无效、无法解析或指向受限地址") from None


def _background(function):
    # DNS/连接池不支持取消；daemon 不会让进程退出再等一个系统级 DNS/连接期限。
    future = concurrent.futures.Future()

    def run():
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(function())
        except BaseException as exc:
            future.set_exception(exc)

    threading.Thread(target=run, name="avelis-webhook-io", daemon=True).start()
    return future


def _resolve_before_deadline(url, deadline):
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError
    # 不用 asyncio 默认 executor：asyncio.run 退出会等待那个 executor 的阻塞 DNS。
    # 同 URL 复用尚未返回的解析；即使系统 resolver 永不返回也最多留下 64 个 daemon。
    with _DNS_LOCK:
        future = _DNS_IN_FLIGHT.get(url)
        if future is None:
            slots = _DNS_SLOTS
            if not slots.acquire(blocking=False):
                raise TimeoutError

            def resolve():
                try:
                    return validate_url(url)
                finally:
                    with _DNS_LOCK:
                        _DNS_IN_FLIGHT.pop(url, None)
                    slots.release()

            future = _background(resolve)
            _DNS_IN_FLIGHT[url] = future
    return future.result(timeout=remaining)


def send_webhook(
    subscription: WebhookSubscription,
    outbox: EventOutbox,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_seconds: float = _REQUEST_TIMEOUT_SECONDS,
    deadline: float | None = None,
) -> None:
    """每次请求重新检查 URL；连接已验证的 IP，保留 HTTP Host 和 TLS 证书域名。"""
    deadline = min(
        monotonic() + min(_REQUEST_TIMEOUT_SECONDS, timeout_seconds), deadline if deadline is not None else float("inf")
    )
    try:
        addresses = _resolve_before_deadline(subscription.url, deadline)
    except ValueError:
        raise DeliveryError("url_rejected") from None
    except TimeoutError:
        raise DeliveryError("timeout") from None
    original = httpx.URL(subscription.url)
    # 独立客户端不复用另一域名的 IP 连接；sni_hostname 同时用于 TLS SNI 和证书域名校验。
    try:
        payload = event_payload(GovernanceEvent.model_validate(outbox.payload))
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    except Exception:
        raise DeliveryError("invalid_payload") from None
    timestamp = str(int(time.time()))
    try:
        secret = decrypt_secret(subscription.secret_enc)
        signature = hmac.new(
            secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256
        ).hexdigest()
        headers = {
            "Host": original.netloc.decode("ascii"),
            "Content-Type": "application/json",
            "X-Avelis-Event": payload["type"],
            "X-Avelis-Delivery": outbox.id,
            "X-Avelis-Signature": f"sha256={signature}",
            "X-Avelis-Timestamp": timestamp,
        }
        asyncio.run(_post_webhook(original, addresses, body, headers, deadline, transport))
    except DeliveryError:
        raise
    except (TimeoutError, httpx.TimeoutException):
        raise DeliveryError("timeout") from None
    except httpx.HTTPError:
        raise DeliveryError("network_error") from None
    except Exception:
        raise DeliveryError("delivery_error") from None


async def _post_webhook(original, addresses, body, headers, deadline, transport):
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise DeliveryError("timeout")
    # HTTPX 的各阶段超时不等于总耗时；外层取消同时限制连接、TLS、写入和响应头等待。
    async with asyncio.timeout(remaining):
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False, transport=transport) as client:
            for index, address in enumerate(addresses):
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise DeliveryError("timeout")
                try:
                    # stream 避免读取无限响应体；所有候选地址共享同一个请求期限。
                    async with client.stream(
                        "POST",
                        original.copy_with(host=address),
                        content=body,
                        headers=headers,
                        timeout=httpx.Timeout(min(_REQUEST_TIMEOUT_SECONDS, remaining), connect=min(3.0, remaining)),
                        extensions={"sni_hostname": original.raw_host.decode("ascii")},
                    ) as response:
                        if not 200 <= response.status_code < 300:
                            raise DeliveryError(f"http_{response.status_code}")
                        return
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    # 仅建立连接失败时才换已验证 IP；写出请求后的异常留给发件箱下一次重试。
                    if index + 1 == len(addresses):
                        raise


def _due_filters(now, handled, excluded_subscriptions):
    return (
        EventOutbox.status == "pending",
        EventOutbox.next_attempt_at <= now,
        WebhookSubscription.enabled.is_(True),
        EventOutbox.id.not_in(handled),
        EventOutbox.subscription_id.not_in(excluded_subscriptions),
    )


def _due_statement(
    now,
    handled: set[str],
    blocked_subscriptions: set[str] | frozenset[str] = frozenset(),
    *,
    include_deferred: bool = True,
):
    stmt = (
        select(EventOutbox, WebhookSubscription)
        .join(WebhookSubscription, EventOutbox.subscription_id == WebhookSubscription.id)
        .where(*_due_filters(now, handled, blocked_subscriptions))
    )
    if not include_deferred:
        stmt = stmt.where(or_(EventOutbox.last_error.is_(None), EventOutbox.last_error != BUDGET_TIMEOUT))
    return (
        stmt
        # 被调度切短过的记录排在最前，作为一轮的第一条拿到整轮预算，保证它们最终有一次完整尝试。
        .order_by(
            # last_error 可为 NULL：PostgreSQL 的 DESC 默认 NULLS FIRST，用非空排序键避免新记录插到前面。
            case((EventOutbox.last_error == BUDGET_TIMEOUT, 0), else_=1),
            EventOutbox.next_attempt_at,
            EventOutbox.created_at,
            EventOutbox.id,
        )
        .limit(1)
        .with_for_update(skip_locked=True, key_share=True)
    )


@dataclass
class _Claim:
    connection: object
    outbox: EventOutbox
    subscription: WebhookSubscription
    locked: bool = False


def _transaction_deadline(session, deadline):
    if session.bind.dialect.name == "postgresql":
        milliseconds = max(1, int((deadline - monotonic()) * 1000))
        session.execute(
            text("SELECT set_config('statement_timeout', :timeout, true), set_config('lock_timeout', :timeout, true)"),
            {"timeout": f"{milliseconds}ms"},
        )


def _release_claim(claim):
    connection = claim.connection
    try:
        if claim.locked:
            with Session(connection) as session:
                _transaction_deadline(session, monotonic() + 1.0)
                session.execute(
                    text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"),
                    {"key": f"webhook:{claim.subscription.id}"},
                )
                session.commit()
    except BaseException:
        # 会话级锁不能随连接返回池；解锁失败就丢弃连接，由 PostgreSQL 回收锁。
        connection.invalidate()
        raise
    finally:
        connection.close()


def _claim_due(now, handled, excluded, include_deferred, deadline):
    connection = get_engine().connect()
    claim = None
    try:
        with Session(connection, expire_on_commit=False) as session:
            _transaction_deadline(session, deadline)
            while monotonic() < deadline:
                row = session.execute(_due_statement(now, handled, excluded, include_deferred=include_deferred)).first()
                if row is None:
                    break
                outbox, subscription = row
                locked = connection.dialect.name == "postgresql"
                if locked and not session.scalar(
                    text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"),
                    {"key": f"webhook:{subscription.id}"},
                ):
                    # 忙订阅不占槽；不等待别的 worker 的 HTTP，也不让它挡住健康订阅。
                    excluded.add(subscription.id)
                    continue
                claim = _Claim(connection, outbox, subscription, locked)
                break
            session.commit()  # HTTP 前立即释放 outbox 和 subscription 的行锁。
        if claim is not None and monotonic() >= deadline:
            _release_claim(claim)
            return None
        if claim is None:
            connection.close()
        return claim
    except BaseException:
        if claim is not None:
            _release_claim(claim)
        else:
            connection.close()
        raise


def _finish_claim(claim, error, defer, settings):
    with Session(claim.connection, expire_on_commit=False) as session:
        # 结果短事务有自己的小期限，不让管理员持锁把已结束的 HTTP 拖成无限等待。
        _transaction_deadline(session, monotonic() + 1.0)
        outbox = session.scalar(
            select(EventOutbox).where(EventOutbox.id == claim.outbox.id).with_for_update(key_share=True)
        )
        # 管理员可能已删除订阅和级联发件箱，不能用旧对象 merge 回去。
        if outbox is None or outbox.status != "pending":
            return
        if defer:
            outbox.last_error = BUDGET_TIMEOUT
        else:
            outbox.attempts += 1
            outbox.last_error = error
            if error is None:
                outbox.status = "delivered"
                outbox.delivered_at = utcnow()
                outbox.dead_at = None
            elif outbox.attempts >= settings.webhook_max_attempts:
                outbox.status = "dead"
                outbox.dead_at = utcnow()
            else:
                minutes = min(2 ** min(outbox.attempts - 1, 30), settings.webhook_retry_max_minutes)
                outbox.next_attempt_at = utcnow() + timedelta(minutes=minutes)
        session.commit()


def _deliver_claim(claim, transport, deadline, settings):
    error = None
    try:
        timeout_seconds = min(_REQUEST_TIMEOUT_SECONDS, deadline - monotonic())
        if timeout_seconds <= 0:
            return claim.subscription.id, "deadline", False
        try:
            send_webhook(
                claim.subscription,
                claim.outbox,
                transport=transport,
                timeout_seconds=timeout_seconds,
                deadline=deadline,
            )
        except DeliveryError as exc:
            error = str(exc)
        defer = (
            error == "timeout"
            and timeout_seconds < _REQUEST_TIMEOUT_SECONDS
            and claim.outbox.last_error != BUDGET_TIMEOUT
        )
        _finish_claim(claim, error, defer, settings)
        return claim.subscription.id, error, defer
    except Exception:
        logger.error("Webhook outbox transaction failed")
        return claim.subscription.id, "transaction_error", False
    finally:
        try:
            _release_claim(claim)
        finally:
            _DELIVERY_SLOTS.release()


async def _claim_before_deadline(function, deadline):
    if not _CLAIM_SLOTS.acquire(blocking=False):
        return None

    def claim():
        try:
            return function()
        finally:
            _CLAIM_SLOTS.release()

    future = _background(claim)
    try:
        return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), max(0, deadline - monotonic()))
    except TimeoutError:
        # 池获取/连接建立也计入整轮期限；迟到的认领只解锁，不再发送。
        def release_late(done):
            try:
                claim = done.result()
                if claim is not None:
                    _release_claim(claim)
            except BaseException:
                logger.error("Webhook outbox transaction failed")

        future.add_done_callback(release_late)
        return None


async def _deliver_due(batch_size, transport, budget_seconds, settings):
    deadline = monotonic() + budget_seconds
    handled, blocked, served = set(), set(), set()
    failures = {}
    active = {}
    attempts = 0

    def completed():
        for future in list(active):
            if not future.done():
                continue
            subscription_id = active.pop(future)
            try:
                _identifier, error, deferred = future.result()
            except BaseException:
                logger.error("Webhook outbox transaction failed")
                blocked.add(subscription_id)
                continue
            if error is None:
                failures[subscription_id] = 0
            else:
                failures[subscription_id] = failures.get(subscription_id, 0) + 1
                if deferred or error in ("timeout", "transaction_error") or failures[subscription_id] >= 2:
                    blocked.add(subscription_id)

    while attempts < batch_size and monotonic() < deadline:
        completed()
        if len(active) >= settings.webhook_concurrency:
            done, _pending = await asyncio.wait(
                active, timeout=max(0, deadline + 1.0 - monotonic()), return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                break
            continue
        if not _DELIVERY_SLOTS.acquire(blocking=False):
            break
        excluded = blocked | served | set(active.values())
        try:
            claim = await _claim_before_deadline(
                lambda: _claim_due(utcnow(), handled, excluded, attempts < settings.webhook_concurrency, deadline),
                deadline,
            )
        except Exception:
            _DELIVERY_SLOTS.release()
            logger.error("Webhook outbox transaction failed")
            break
        if claim is None:
            _DELIVERY_SLOTS.release()
            if served:
                served.clear()
                continue
            if active:
                done, _pending = await asyncio.wait(
                    active, timeout=max(0, deadline + 1.0 - monotonic()), return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    break
                continue
            break
        remaining = deadline - monotonic()
        if remaining <= 0:
            _background(lambda: _release_claim(claim))
            _DELIVERY_SLOTS.release()
            break
        handled.add(claim.outbox.id)
        served.add(claim.subscription.id)
        attempts += 1
        future = asyncio.wrap_future(
            _background(lambda claim=claim: _deliver_claim(claim, transport, deadline, settings))
        )
        active[future] = claim.subscription.id
    if active:
        # 请求已在轮期限停止；给短结果事务/解锁最多一秒，数据库断线不能卡死调度。
        # 迟到线程仍持有租约直到 finally，下一轮不会重叠发送同一订阅。
        await asyncio.wait(active, timeout=max(0, deadline + 1.0 - monotonic()))
        completed()
    return attempts


def deliver_due(
    batch_size: int = 50, *, transport: httpx.AsyncBaseTransport | None = None, budget_seconds: float = 5.0
) -> int:
    """并发请求各占独立连接和短 Session；HTTP 期间只持订阅会话锁，不持行锁或事务。

    断线/进程退出自动释放会话锁。远端已收而结果未提交时仍会重复投递，由事件 ID 去重。
    """
    return asyncio.run(_deliver_due(batch_size, transport, budget_seconds, get_settings()))
