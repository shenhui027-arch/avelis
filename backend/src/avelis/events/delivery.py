"""Webhook 发件箱投递；网络与数据库失败不影响已经提交的业务审计。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import time
from datetime import timedelta
from time import monotonic
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func, select

from avelis.config import get_settings
from avelis.db import session_scope, utcnow
from avelis.events import GovernanceEvent, event_payload
from avelis.models import EventOutbox, WebhookSubscription
from avelis.security import decrypt_secret

logger = logging.getLogger(__name__)
_REQUEST_TIMEOUT_SECONDS = 5.0
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


def send_webhook(
    subscription: WebhookSubscription,
    outbox: EventOutbox,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_seconds: float = _REQUEST_TIMEOUT_SECONDS,
) -> None:
    """每次请求重新检查 URL；连接已验证的 IP，保留 HTTP Host 和 TLS 证书域名。"""
    deadline = monotonic() + min(_REQUEST_TIMEOUT_SECONDS, timeout_seconds)
    try:
        addresses = validate_url(subscription.url)
    except ValueError:
        raise DeliveryError("url_rejected") from None
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


def _due_statement(now, handled: set[str], blocked_subscriptions: set[str] | frozenset[str] = frozenset()):
    return (
        select(EventOutbox, WebhookSubscription)
        .join(WebhookSubscription, EventOutbox.subscription_id == WebhookSubscription.id)
        .where(*_due_filters(now, handled, blocked_subscriptions))
        .order_by(EventOutbox.next_attempt_at, EventOutbox.created_at, EventOutbox.id)
        .limit(1)
        .with_for_update(skip_locked=True, key_share=True)
    )


def _due_subscription_count(session, now, handled, excluded_subscriptions):
    # 只统计，不认领；被其他事务锁住的订阅也只会使预算预留更保守，不等待它们。
    return session.scalar(
        select(func.count(func.distinct(EventOutbox.subscription_id)))
        .join(WebhookSubscription, EventOutbox.subscription_id == WebhookSubscription.id)
        .where(*_due_filters(now, handled, excluded_subscriptions))
    )


def deliver_due(
    batch_size: int = 50, *, transport: httpx.AsyncBaseTransport | None = None, budget_seconds: float = 5.0
) -> int:
    """逐条提交并释放行锁；远端已收而本地未提交时可重复投递，由事件 ID 去重。"""
    settings = get_settings()
    deadline = monotonic() + budget_seconds
    handled: set[str] = set()
    failures: dict[str, int] = {}
    blocked_subscriptions: set[str] = set()
    served_subscriptions: set[str] = set()
    attempts = 0
    for _ in range(batch_size):
        if monotonic() >= deadline:
            break
        try:
            with session_scope() as session:
                now = utcnow()
                excluded = blocked_subscriptions | served_subscriptions
                row = session.execute(_due_statement(now, handled, excluded)).first()
                if row is None and served_subscriptions:
                    # 每个可认领订阅先投一条，全部轮过后才能继续下一条。
                    served_subscriptions.clear()
                    excluded = blocked_subscriptions
                    row = session.execute(_due_statement(now, handled, excluded)).first()
                if row is None:
                    break
                outbox, subscription = row
                subscription_count = _due_subscription_count(session, now, handled, excluded)
                remaining = deadline - monotonic()
                if remaining <= 0:
                    break
                # 当前订阅只能使用本轮的一份预算，给尚未轮到的订阅留出发送机会。
                waiting = min(max(1, subscription_count), batch_size - attempts)
                timeout_seconds = min(_REQUEST_TIMEOUT_SECONDS, remaining / waiting)
                handled.add(outbox.id)
                served_subscriptions.add(subscription.id)
                outbox.attempts += 1
                attempts += 1
                try:
                    send_webhook(subscription, outbox, transport=transport, timeout_seconds=timeout_seconds)
                except DeliveryError as exc:
                    failures[subscription.id] = failures.get(subscription.id, 0) + 1
                    if str(exc) == "timeout" or failures[subscription.id] >= 2:
                        blocked_subscriptions.add(subscription.id)
                    outbox.last_error = str(exc)
                    if outbox.attempts >= settings.webhook_max_attempts:
                        outbox.status = "dead"
                    else:
                        # 先截断指数，配置再大也不生成巨大整数。
                        minutes = min(2 ** min(outbox.attempts - 1, 30), settings.webhook_retry_max_minutes)
                        outbox.next_attempt_at = utcnow() + timedelta(minutes=minutes)
                else:
                    failures[subscription.id] = 0
                    outbox.status = "delivered"
                    outbox.delivered_at = utcnow()
                    outbox.last_error = None
        except Exception:
            # 数据库故障也不能停止 worker。异常正文可能含连接串，不写进日志。
            logger.error("Webhook outbox transaction failed")
    return attempts
