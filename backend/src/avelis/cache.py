"""Redis（演示用 Valkey）：任务唤醒、令牌吊销、答案缓存、分析运行中的实时过程（analysis/live.py）。

Redis 不是事实来源：丢失只影响性能与重试。答案缓存键包含 SQL、指标版本与依赖指纹、资产版本、
权限范围和数据新鲜度，任何一项变化都会自然错开；另外按指标版本建标签集合，受影响时主动清除。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

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

_LOCAL_RATE_LOCK = threading.Lock()
_LOCAL_RATE_MAX_ENTRIES = 4096
_LOCAL_RATE_COUNTERS: OrderedDict[str, tuple[int, float]] = OrderedDict()


def clear_local_rate_limits() -> None:
    """清空进程内限流回退计数（供测试隔离使用）。"""
    with _LOCAL_RATE_LOCK:
        _LOCAL_RATE_COUNTERS.clear()


def fixed_window_rate_limit(prefix: str, key: str, limit: int, *, window_seconds: int = 60) -> tuple[bool, int, int]:
    """固定窗口频次限流：返回 (allowed, count, retry_after)。limit <= 0 时不限流。Redis 异常时降级到本地有界计数。"""
    if limit <= 0:
        return True, 0, 0
    window = max(int(window_seconds), 1)
    now = time.time()
    bucket = int(now) // window
    retry_after = max(window - int(now) % window, 1)
    redis_key = f"avelis:rl:{prefix}:{key}:{bucket}"
    try:
        with client().pipeline(transaction=True) as pipe:
            pipe.incr(redis_key)
            pipe.expire(redis_key, window + 1, nx=True)
            count, _ = pipe.execute()
        count_i = int(count)
        return count_i <= limit, count_i, retry_after
    except RedisError:
        logger.warning("Redis 不可用，固定窗口限流降级为进程内计数器（prefix=%s）", prefix)
    mono_now = time.monotonic()
    deadline = mono_now + retry_after
    with _LOCAL_RATE_LOCK:
        expired = [k for k, (_, exp) in _LOCAL_RATE_COUNTERS.items() if exp <= mono_now]
        for k in expired:
            _LOCAL_RATE_COUNTERS.pop(k, None)
        entry = _LOCAL_RATE_COUNTERS.get(redis_key)
        if entry is None or entry[1] <= mono_now:
            count_i = 1
            _LOCAL_RATE_COUNTERS[redis_key] = (count_i, deadline)
        else:
            count_i = entry[0] + 1
            _LOCAL_RATE_COUNTERS[redis_key] = (count_i, entry[1])
            _LOCAL_RATE_COUNTERS.move_to_end(redis_key)
        while len(_LOCAL_RATE_COUNTERS) > _LOCAL_RATE_MAX_ENTRIES:
            _LOCAL_RATE_COUNTERS.popitem(last=False)
    return count_i <= limit, count_i, retry_after


def clear_local_fallbacks() -> None:
    """清空进程内防重放与限流回退缓存（供测试隔离使用）。"""
    with _LOCAL_ONCE_LOCK:
        _LOCAL_ONCE_MARKERS.clear()
    clear_local_rate_limits()


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


_LITERAL_RE = re.compile(
    r"\d{4}[-/年]\d{1,2}(?:[-/月]\d{1,2}日?)?|\d{1,2}月\d{1,2}日|\d{4}年|\d{4}[-]?Q[1-4]|"
    r"\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+|"
    r"今年|去年|前年|明年|本月|上月|上个月|下月|下个月|本季度|上季度|下季度|本周|上周|下周|昨天|今天|前天|"
    r"同比|环比|累计|从高到低|从低到高|降序|升序|最高|最低|最大|最小|前\d+|倒数"
)
_QUERY_FILLER_TOKENS = frozenset(
    {
        "请",
        "帮",
        "我",
        "的",
        "了",
        "吗",
        "呢",
        "和",
        "与",
        "及",
        "在",
        "按",
        "看",
        "查",
        "下",
        "一",
        "统计",
        "查询",
        "查看",
        "分析",
        "显示",
        "多少",
        "是多少",
        "情况",
        "如何",
        "怎样",
        "什么",
        "一下",
        "请帮",
        "帮我",
        "我想",
        "请问",
        "地区",
        "区域",
        "分组",
        "排名",
        "总额",
        "总计",
    }
)


def extract_query_literals(text: str) -> tuple[str, ...]:
    """提取查询中的数值、日期、相对时间、同比/环比与排序方向字面量，防止语义缓存误命中。"""
    return tuple(sorted(_LITERAL_RE.findall((text or "").strip())))


def _has_entity_token_mismatch(q_norm: str, candidate_norm: str) -> bool:
    """若两句之间存在不重叠的实体/维度词差异（如 区域甲 vs 区域乙、维度甲 vs 维度乙），拒绝语义缓存命中。"""
    from avelis.retrieval_hybrid import tokenize

    q_words = {
        w for w in tokenize(q_norm) if len(w) >= 2 and w not in _QUERY_FILLER_TOKENS and not _LITERAL_RE.fullmatch(w)
    }
    c_words = {
        w
        for w in tokenize(candidate_norm)
        if len(w) >= 2 and w not in _QUERY_FILLER_TOKENS and not _LITERAL_RE.fullmatch(w)
    }
    # 存在互相排斥的多字实词（两句各含有对方未出现的多字实词），典型为城市/大区/产品/指标替换
    only_q = q_words - c_words
    only_c = c_words - q_words
    if only_q and only_c:
        # 只要两句文本长度接近或仅局部实体替换（例如同时存在独有 2-gram），一律阻止误命中
        return True
    return False


def _semcache_index_key(scope_sig: str) -> str:
    return f"avelis:semcache:idx:{scope_sig}"


def semantic_cache_lookup(
    scope_sig: str,
    question: str,
    *,
    query_vec: Sequence[float] | None,
    threshold: float,
    max_entries: int = 200,
) -> dict[str, Any] | None:
    """按权限/版本作用域查找语义相似查询；字面量不等必不命中，Redis 异常静默降级返回 None。"""
    from avelis.retrieval_hybrid import SearchDocument, cosine_similarity, tfidf_scores, tokenize

    q_norm = " ".join((question or "").strip().split())
    if not q_norm:
        return None
    q_literals = extract_query_literals(q_norm)
    q_tokens = set(tokenize(q_norm))
    idx_key = _semcache_index_key(scope_sig)
    try:
        raw_index = client().get(idx_key)
    except RedisError:
        logger.warning("Redis 不可用，跳过语义缓存查询")
        return None
    if not raw_index:
        return None
    try:
        entries = json.loads(raw_index)
    except (ValueError, TypeError):
        return None
    if not isinstance(entries, list) or not entries:
        return None

    candidates = [
        entry
        for entry in entries[-max_entries:]
        if isinstance(entry, dict)
        and tuple(entry.get("literals") or ()) == q_literals
        and not _has_entity_token_mismatch(q_norm, entry.get("q_norm", ""))
    ]
    if not candidates:
        return None

    docs = [SearchDocument(key=entry["id"], fields=[(1.0, entry.get("q_norm", ""))]) for entry in candidates]
    tfidf_map = tfidf_scores(q_norm, docs)

    best_entry: dict[str, Any] | None = None
    best_score = 0.0
    for entry in candidates:
        c_tokens = set(entry.get("tokens") or [])
        union = len(q_tokens | c_tokens)
        jaccard = (len(q_tokens & c_tokens) / union) if union else 0.0
        lex_sim = max(tfidf_map.get(entry["id"], 0.0), jaccard)
        c_vec = entry.get("vec")
        if query_vec and isinstance(c_vec, list) and len(c_vec) == len(query_vec):
            cos_sim = max(0.0, cosine_similarity(query_vec, c_vec))
            score = 0.7 * cos_sim + 0.3 * lex_sim
        else:
            score = lex_sim
        if score >= threshold and score > best_score:
            best_score = score
            best_entry = entry

    if best_entry is None:
        return None

    try:
        payload = cache_get(best_entry["item_key"])
    except (RedisError, ValueError, TypeError):
        logger.warning("Redis 不可用，语义缓存读取条目失败")
        return None
    if payload is None:
        return None
    return {**payload, "similarity": best_score}


def semantic_cache_store(
    scope_sig: str,
    question: str,
    *,
    query_vec: Sequence[float] | None,
    payload: dict[str, Any],
    ttl_seconds: int,
    max_entries: int,
    tags: list[str],
) -> None:
    """写入权限/版本隔离的语义缓存并挂接 metric_version/asset 标签以便失效。"""
    from avelis.retrieval_hybrid import tokenize

    q_norm = " ".join((question or "").strip().split())
    if not q_norm or ttl_seconds <= 0 or max_entries <= 0:
        return
    entry_id = uuid.uuid4().hex
    item_key = f"avelis:semcache:item:{entry_id}"
    idx_key = _semcache_index_key(scope_sig)
    entry = {
        "id": entry_id,
        "item_key": item_key,
        "q_norm": q_norm,
        "literals": list(extract_query_literals(q_norm)),
        "tokens": sorted(set(tokenize(q_norm))),
        "vec": list(query_vec) if query_vec else None,
    }
    try:
        cache_put(item_key, payload, ttl_seconds, tags)
        raw_index = client().get(idx_key)
        entries = json.loads(raw_index) if raw_index else []
        if not isinstance(entries, list):
            entries = []
        entries = [e for e in entries if isinstance(e, dict) and e.get("q_norm") != q_norm]
        entries.append(entry)
        if len(entries) > max_entries:
            entries = entries[-max_entries:]
        client().set(idx_key, json.dumps(entries, ensure_ascii=False), ex=ttl_seconds * 2)
    except (RedisError, ValueError, TypeError):
        logger.warning("Redis 不可用，跳过语义缓存写入")
