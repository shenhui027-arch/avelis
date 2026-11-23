"""Redis（演示用 Valkey）：任务唤醒、令牌吊销、答案缓存、分析运行中的实时过程（analysis/live.py）。

Redis 不是事实来源：丢失只影响性能与重试。答案缓存键包含 SQL、指标版本与依赖指纹、资产版本、
权限范围和数据新鲜度，任何一项变化都会自然错开；另外按指标版本建标签集合，受影响时主动清除。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from functools import lru_cache
from typing import Any, Iterator

import redis
from redis.exceptions import RedisError

from avelis.config import get_settings
from avelis.connectors.base import sha256_json

JOB_QUEUE = "avelis:jobs"
WORKER_HEARTBEAT = "avelis:worker:heartbeat"
logger = logging.getLogger(__name__)


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
    try:
        client().set(f"avelis:revoked:{jti}", "1", ex=max(ttl_seconds, 1))
    except RedisError:
        logger.warning("Redis 不可用，跳过令牌吊销缓存写入")


def is_revoked(jti: str) -> bool:
    try:
        return client().exists(f"avelis:revoked:{jti}") == 1
    except RedisError:
        logger.warning("Redis 不可用，降级跳过吊销缓存校验")
        return False


def rate_limit_locked_seconds(prefix: str, key: str, max_failures: int) -> int:
    """连续失败达到上限时返回剩余锁定秒数，否则 0；Redis 不可用时降级放行。"""
    if max_failures <= 0:
        return 0
    name = f"avelis:{prefix}:{key}"
    try:
        with client().pipeline(transaction=True) as pipe:
            pipe.get(name)
            pipe.ttl(name)
            count, ttl = pipe.execute()
    except RedisError:
        logger.warning("Redis 不可用，降级跳过限流锁定检查（prefix=%s）", prefix)
        return 0
    if count is not None and int(count) >= max_failures:
        return max(int(ttl), 1) if ttl is not None else 1
    return 0


def record_rate_limit_failure(prefix: str, key: str, window_seconds: int) -> int:
    name = f"avelis:{prefix}:{key}"
    try:
        with client().pipeline(transaction=True) as pipe:
            pipe.incr(name)
            pipe.expire(name, max(window_seconds, 1), nx=True)
            count, _ = pipe.execute()
        return int(count)
    except RedisError:
        logger.warning("Redis 不可用，跳过失败计数写入（prefix=%s）", prefix)
        return 0


def clear_rate_limit(prefix: str, key: str) -> None:
    try:
        client().delete(f"avelis:{prefix}:{key}")
    except RedisError:
        logger.warning("Redis 不可用，跳过限流计数清理（prefix=%s）", prefix)


def login_locked_seconds(key: str, max_failures: int) -> int:
    """连续失败达到上限时返回剩余锁定秒数，否则 0。"""
    return rate_limit_locked_seconds("login-fail", key, max_failures)


def record_login_failure(key: str, window_seconds: int) -> int:
    return record_rate_limit_failure("login-fail", key, window_seconds)


def clear_login_failures(key: str) -> None:
    clear_rate_limit("login-fail", key)


_LOCAL_ONCE_LOCK = threading.Lock()
_LOCAL_ONCE_MAX_ENTRIES = 4096
_LOCAL_ONCE_MARKERS: OrderedDict[str, float] = OrderedDict()


def clear_local_fallbacks() -> None:
    """清空进程内防重放回退缓存（供测试隔离使用）。"""
    with _LOCAL_ONCE_LOCK:
        _LOCAL_ONCE_MARKERS.clear()


def _local_once_prune_locked(now: float) -> None:
    expired = [k for k, deadline in _LOCAL_ONCE_MARKERS.items() if deadline <= now]
    for k in expired:
        _LOCAL_ONCE_MARKERS.pop(k, None)
    while len(_LOCAL_ONCE_MARKERS) > _LOCAL_ONCE_MAX_ENTRIES:
        _LOCAL_ONCE_MARKERS.popitem(last=False)


def _local_once_is_marked(name: str) -> bool:
    now = time.monotonic()
    with _LOCAL_ONCE_LOCK:
        deadline = _LOCAL_ONCE_MARKERS.get(name)
        if deadline is None:
            return False
        if deadline <= now:
            _LOCAL_ONCE_MARKERS.pop(name, None)
            return False
        return True


def _local_once_mark(name: str, ttl_seconds: int) -> None:
    now = time.monotonic()
    deadline = now + max(int(ttl_seconds), 1)
    with _LOCAL_ONCE_LOCK:
        _local_once_prune_locked(now)
        _LOCAL_ONCE_MARKERS[name] = deadline
        _LOCAL_ONCE_MARKERS.move_to_end(name)
        while len(_LOCAL_ONCE_MARKERS) > _LOCAL_ONCE_MAX_ENTRIES:
            _LOCAL_ONCE_MARKERS.popitem(last=False)


def _local_once_try_set(name: str, ttl_seconds: int) -> bool:
    now = time.monotonic()
    deadline = now + max(int(ttl_seconds), 1)
    with _LOCAL_ONCE_LOCK:
        existing = _LOCAL_ONCE_MARKERS.get(name)
        if existing is not None:
            if existing > now:
                return False
            _LOCAL_ONCE_MARKERS.pop(name, None)
        _local_once_prune_locked(now)
        _LOCAL_ONCE_MARKERS[name] = deadline
        _LOCAL_ONCE_MARKERS.move_to_end(name)
        while len(_LOCAL_ONCE_MARKERS) > _LOCAL_ONCE_MAX_ENTRIES:
            _LOCAL_ONCE_MARKERS.popitem(last=False)
        return True


def _local_once_pop(name: str) -> bool:
    now = time.monotonic()
    with _LOCAL_ONCE_LOCK:
        deadline = _LOCAL_ONCE_MARKERS.pop(name, None)
    return deadline is not None and deadline > now


def is_marked(prefix: str, key: str) -> bool:
    """检查一次性防重放键是否已存在（优先检查进程内有界 TTL 回退缓存）。"""
    name = f"avelis:{prefix}:{key}"
    if _local_once_is_marked(name):
        return True
    try:
        if client().exists(name) != 1:
            return False
    except RedisError:
        logger.warning("Redis 不可用，降级仅检查本地一次性防重放缓存（prefix=%s）", prefix)
        return False
    ttl_seconds = 300
    try:
        ttl_raw = int(client().ttl(name))
        if ttl_raw > 0:
            ttl_seconds = ttl_raw
    except (RedisError, KeyError, TypeError, ValueError):
        pass
    _local_once_mark(name, ttl_seconds)
    return True


def try_set_once(prefix: str, key: str, ttl_seconds: int) -> bool:
    """原子写入一次性防重放键；首次写入返回 True，已存在（重放）返回 False。

    Redis 正常时原子 NX 写入并同步镜像到进程内有界 TTL 缓存；Redis 不可用时降级到进程内有界 TTL 缓存。
    """
    ttl = max(int(ttl_seconds), 1)
    name = f"avelis:{prefix}:{key}"
    if _local_once_is_marked(name):
        return False
    try:
        created = bool(client().set(name, "1", ex=ttl, nx=True))
    except RedisError:
        logger.warning("Redis 不可用，降级写入本地一次性防重放缓存（prefix=%s）", prefix)
        return _local_once_try_set(name, ttl)
    _local_once_mark(name, ttl)
    return created


def mark_once(prefix: str, key: str, ttl_seconds: int) -> bool:
    """原子写入一次性防重放键；成功返回 True，已存在（重放）返回 False。"""
    return try_set_once(prefix, key, ttl_seconds)


def consume_once(prefix: str, key: str) -> bool:
    """原子消费一次性键；存在并删除返回 True，缺失返回 False，Redis 不可用时回退到本地有界缓存。"""
    name = f"avelis:{prefix}:{key}"
    local_existed = _local_once_pop(name)
    try:
        redis_deleted = int(client().delete(name)) == 1
        return redis_deleted or local_existed
    except RedisError:
        logger.warning("Redis 不可用，降级消费本地一次性防重放缓存（prefix=%s）", prefix)
        return local_existed


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
