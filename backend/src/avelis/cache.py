"""Redis（演示用 Valkey）：任务唤醒、采集互斥锁、令牌吊销、答案缓存。

Redis 不是事实来源：丢失只影响性能与重试。答案缓存键包含 SQL、指标版本与依赖指纹、资产版本、
权限范围和数据新鲜度，任何一项变化都会自然错开；另外按指标版本建标签集合，受影响时主动清除。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from functools import lru_cache
from typing import Any, Iterator

import redis

from avelis.config import get_settings
from avelis.connectors.base import sha256_json

JOB_QUEUE = "avelis:jobs"


@lru_cache(maxsize=2)
def _client(url: str) -> redis.Redis:
    return redis.Redis.from_url(url, decode_responses=True, socket_timeout=5, socket_connect_timeout=3)


def client() -> redis.Redis:
    return _client(get_settings().redis_url)


class LockBusy(Exception):
    pass


@contextmanager
def lock(name: str, ttl_seconds: int = 900) -> Iterator[None]:
    lk = client().lock(f"avelis:lock:{name}", timeout=ttl_seconds, blocking=False)
    if not lk.acquire(blocking=False):
        raise LockBusy(name)
    try:
        yield
    finally:
        try:
            lk.release()
        except redis.exceptions.LockError:
            pass


def revoke_token(jti: str, ttl_seconds: int) -> None:
    client().set(f"avelis:revoked:{jti}", "1", ex=max(ttl_seconds, 1))


def is_revoked(jti: str) -> bool:
    return client().exists(f"avelis:revoked:{jti}") == 1


def answer_cache_key(parts: dict[str, Any]) -> str:
    return "avelis:answer:" + sha256_json(parts)


def cache_get(key: str) -> dict[str, Any] | None:
    raw = client().get(key)
    return json.loads(raw) if raw else None


def cache_put(key: str, value: dict[str, Any], ttl_seconds: int, tags: list[str]) -> None:
    pipe = client().pipeline()
    pipe.set(key, json.dumps(value, ensure_ascii=False, default=str), ex=ttl_seconds)
    for tag in tags:
        pipe.sadd(f"avelis:tag:{tag}", key)
        pipe.expire(f"avelis:tag:{tag}", ttl_seconds * 2)
    pipe.execute()


def invalidate_tag(tag: str) -> int:
    c = client()
    keys = list(c.smembers(f"avelis:tag:{tag}"))
    if keys:
        c.delete(*keys)
    c.delete(f"avelis:tag:{tag}")
    return len(keys)
