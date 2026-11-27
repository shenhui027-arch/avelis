"""多实例 worker 心跳（#159）：心跳归属单个 worker，退出只清自己；健康接口汇总仍存活的 worker，兼容旧版共享键。

用只实现所需命令的内存 Redis 驱动 cache 里的心跳函数，不连接外部服务。"""

from datetime import datetime, timedelta, timezone

import pytest

from avelis import cache

T0 = datetime(2026, 11, 27, 8, 0, 0, tzinfo=timezone.utc)


class FakeRedis:
    """内存版 Redis：只支持心跳用到的 SET/GET/ZADD/ZREM/ZREMRANGEBYSCORE/ZREVRANGEBYSCORE，过期按注入的时钟判断。"""

    def __init__(self, clock):
        self.clock = clock
        self.strings: dict[str, tuple[str, float | None]] = {}
        self.zsets: dict[str, dict[str, float]] = {}

    def _alive(self, key):
        value = self.strings.get(key)
        if value is None:
            return None
        text, expires = value
        if expires is not None and self.clock().timestamp() >= expires:
            del self.strings[key]
            return None
        return text

    def set(self, key, value, ex=None):
        self.strings[key] = (value, None if ex is None else self.clock().timestamp() + ex)
        return True

    def get(self, key):
        return self._alive(key)

    def delete(self, *keys):  # 新实现不应再调用；保留以便断言旧共享键没有被删
        removed = sum(1 for key in keys if self.strings.pop(key, None) is not None)
        return removed

    def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update({member: float(score) for member, score in mapping.items()})
        return len(mapping)

    def zrem(self, key, *members):
        zset = self.zsets.get(key, {})
        return sum(1 for member in members if zset.pop(member, None) is not None)

    @staticmethod
    def _bound(value, upper):
        if value in ("+inf", float("inf")):
            return float("inf"), False
        if value in ("-inf", float("-inf")):
            return float("-inf"), False
        if isinstance(value, str) and value.startswith("("):
            return float(value[1:]), True
        return float(value), False

    def _in_range(self, score, low, high):
        lo, lo_open = self._bound(low, False)
        hi, hi_open = self._bound(high, True)
        return (score > lo if lo_open else score >= lo) and (score < hi if hi_open else score <= hi)

    def zremrangebyscore(self, key, low, high):
        zset = self.zsets.get(key, {})
        doomed = [member for member, score in zset.items() if self._in_range(score, low, high)]
        for member in doomed:
            del zset[member]
        return len(doomed)

    def zrevrangebyscore(self, key, high, low, start=None, num=None, withscores=False, **_):
        items = sorted(
            ((member, score) for member, score in self.zsets.get(key, {}).items() if self._in_range(score, low, high)),
            key=lambda item: item[1],
            reverse=True,
        )
        if start is not None and num is not None:
            items = items[start : start + num]
        return items if withscores else [member for member, _ in items]


@pytest.fixture
def redis(monkeypatch):
    now = {"t": T0}
    fake = FakeRedis(lambda: now["t"])
    monkeypatch.setattr(cache, "client", lambda: fake)
    fake.now = now
    return fake


def advance(redis, seconds):
    redis.now["t"] += timedelta(seconds=seconds)
    return redis.now["t"]


def test_worker_ids_are_unique_per_process_start():
    first, second = cache.worker_id(), cache.worker_id()
    assert first and second and first != second


def test_stopping_one_worker_keeps_pool_alive_and_last_seen_from_survivor(redis):
    cache.record_worker_heartbeat("a", T0)
    b_seen = advance(redis, 5)
    cache.record_worker_heartbeat("b", b_seen)
    advance(redis, 1)
    cache.remove_worker_heartbeat("a")  # A 正常退出，B 刚上报过
    assert cache.worker_heartbeat_status(redis.now["t"]) == (True, b_seen.isoformat())
    # 新 worker 退出不删旧共享键（只等它过期），否则会抹掉其他 worker 的心跳
    assert redis.get(cache.WORKER_HEARTBEAT) == b_seen.isoformat()


def test_last_worker_stopping_reports_offline_once_legacy_key_expires(redis):
    cache.record_worker_heartbeat("a", T0)
    cache.remove_worker_heartbeat("a")
    # 旧共享键仍在 TTL 内（兼容旧版 app），此时仍判定存活；过期后为不存活
    assert cache.worker_heartbeat_status(advance(redis, 1))[0] is True
    assert cache.worker_heartbeat_status(advance(redis, cache.WORKER_HEARTBEAT_TTL)) == (False, None)


def test_crashed_worker_expires_after_ttl_and_is_purged_on_next_report(redis):
    cache.record_worker_heartbeat("crashed", T0)
    later = advance(redis, cache.WORKER_HEARTBEAT_TTL + 1)  # 异常退出：不再上报，也没有删除
    assert cache.worker_heartbeat_status(later) == (False, None)
    cache.record_worker_heartbeat("b", later)
    assert set(redis.zsets[cache.WORKER_HEARTBEATS]) == {"b"}  # 上报时顺手清掉过期记录
    assert cache.worker_heartbeat_status(later) == (True, later.isoformat())


def test_latest_report_wins_and_score_is_a_unix_timestamp(redis):
    cache.record_worker_heartbeat("a", T0)
    b_seen = advance(redis, 30)
    cache.record_worker_heartbeat("b", b_seen)
    a_seen = advance(redis, 10)
    cache.record_worker_heartbeat("a", a_seen)
    assert redis.zsets[cache.WORKER_HEARTBEATS] == {"a": a_seen.timestamp(), "b": b_seen.timestamp()}
    assert cache.worker_heartbeat_status(a_seen) == (True, a_seen.isoformat())


def test_legacy_worker_heartbeat_still_counts_during_rolling_update(redis):
    # 旧版 worker 只写共享键（ISO 字符串，60 秒过期）
    legacy_seen = T0 + timedelta(seconds=40)
    redis.now["t"] = legacy_seen
    redis.set(cache.WORKER_HEARTBEAT, legacy_seen.isoformat(), ex=60)
    assert cache.worker_heartbeat_status(legacy_seen) == (True, legacy_seen.isoformat())
    # 新 worker 更早上报时，last_seen 取较晚的旧键时间
    redis.zadd(cache.WORKER_HEARTBEATS, {"new": T0.timestamp() + 30})
    assert cache.worker_heartbeat_status(legacy_seen) == (True, legacy_seen.isoformat())


def test_unparseable_legacy_value_still_counts_as_alive(redis):
    redis.set(cache.WORKER_HEARTBEAT, "not-a-time", ex=60)
    alive, _ = cache.worker_heartbeat_status(T0)
    assert alive is True


def test_no_heartbeats_reports_offline(redis):
    assert cache.worker_heartbeat_status(T0) == (False, None)
