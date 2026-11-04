"""投递独立于同步业务扫描；固定一个循环，退出前结束当前投递。"""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from avelis import cache, jobs
from avelis.events import delivery


@pytest.fixture
def worker(monkeypatch):
    handlers = {}
    connection = MagicMock()
    connection.brpop.return_value = (cache.JOB_QUEUE, "job-id")
    monkeypatch.setattr(jobs, "get_settings", lambda: SimpleNamespace(worker_schedule_seconds=60))
    monkeypatch.setattr(jobs.signal, "signal", lambda signum, handler: handlers.__setitem__(signum, handler))
    monkeypatch.setattr(cache, "client", lambda: connection)
    monkeypatch.setattr(jobs, "scan_once", MagicMock())
    monkeypatch.setattr(jobs, "run_job", MagicMock())
    return SimpleNamespace(handlers=handlers, connection=connection)


@pytest.mark.parametrize("blocking_step", ["scan_once", "run_job"])
def test_worker_delivers_while_business_work_is_blocked(monkeypatch, worker, blocking_step):
    business_running = threading.Event()
    delivered = threading.Event()
    delivery_threads = []

    def deliver():
        assert business_running.wait(timeout=2), "业务任务没有开始"
        delivery_threads.append(threading.current_thread())
        delivered.set()

    def business(*args):
        business_running.set()
        try:
            assert delivered.wait(timeout=2), "业务任务阻塞期间未投递"
        finally:
            worker.handlers[jobs.signal.SIGINT]()

    monkeypatch.setattr(delivery, "deliver_due", deliver)
    monkeypatch.setattr(jobs, blocking_step, business)
    jobs.main()

    assert delivered.is_set()
    assert len(delivery_threads) == 1
    assert delivery_threads[0].name == "avelis-webhooks"
    assert not delivery_threads[0].is_alive()
    worker.connection.delete.assert_called_once_with(cache.WORKER_HEARTBEAT)


def test_delivery_loop_recovers_after_failure_and_keeps_start_interval(monkeypatch, caplog):
    class Stop:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, timeout):
            waits.append(timeout)
            return len(waits) == 2

    waits, calls = [], []
    clock = iter([10.0, 13.0, 70.0, 75.0])
    monkeypatch.setattr(jobs.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(jobs, "get_settings", lambda: SimpleNamespace(worker_schedule_seconds=60))

    def deliver():
        calls.append(threading.current_thread())
        if len(calls) == 1:
            raise RuntimeError("temporary failure")

    monkeypatch.setattr(delivery, "deliver_due", deliver)
    jobs._deliver_events_loop(Stop())

    assert calls == [threading.current_thread()] * 2
    assert waits == [57.0, 55.0]
    assert "投递治理事件 失败，下一轮重试" in caplog.text


def test_worker_waits_for_active_delivery_before_returning(monkeypatch, worker):
    delivery_started = threading.Event()
    delivery_finished = threading.Event()
    stop_observed = threading.Event()
    delivery_threads = []
    event_factory = threading.Event
    events = []

    class DeliveryStop(threading.Event):
        def set(self):
            super().set()
            stop_observed.set()

    def event():
        # main 创建顺序：心跳 stop、首次心跳 ready、投递 stop。
        result = DeliveryStop() if len(events) == 2 else event_factory()
        events.append(result)
        return result

    monkeypatch.setattr(jobs, "threading", SimpleNamespace(Thread=threading.Thread, Event=event))

    def deliver():
        delivery_threads.append(threading.current_thread())
        delivery_started.set()
        assert stop_observed.wait(timeout=2), "退出未通知投递循环"
        delivery_finished.set()

    def scan():
        assert delivery_started.wait(timeout=2)
        worker.handlers[jobs.signal.SIGINT]()

    monkeypatch.setattr(delivery, "deliver_due", deliver)
    monkeypatch.setattr(jobs, "scan_once", scan)
    jobs.main()

    assert delivery_finished.is_set()
    assert len(delivery_threads) == 1
    assert not delivery_threads[0].is_alive()


def test_delivery_loop_does_not_overlap_or_create_more_threads(monkeypatch):
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    stop = threading.Event()
    calls = []

    def deliver():
        calls.append(threading.current_thread())
        if len(calls) == 1:
            first_started.set()
            assert release_first.wait(timeout=2)
        else:
            second_started.set()
            stop.set()

    # 模拟已经超过调度间隔，下一轮应等上一轮返回，不能并发派发。
    clock = iter([0.0, 61.0, 61.0, 62.0])
    monkeypatch.setattr(jobs.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(jobs, "get_settings", lambda: SimpleNamespace(worker_schedule_seconds=60))
    monkeypatch.setattr(delivery, "deliver_due", deliver)
    thread = threading.Thread(target=jobs._deliver_events_loop, args=(stop,))
    thread.start()
    try:
        assert first_started.wait(timeout=2)
        assert not second_started.is_set()
        assert calls == [thread]
        release_first.set()
        assert second_started.wait(timeout=2)
    finally:
        stop.set()
        release_first.set()
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert calls == [thread, thread]


def test_worker_limits_delivery_shutdown_wait_to_five_seconds(monkeypatch, worker):
    started, release = threading.Event(), threading.Event()
    joins, delivery_threads = [], []
    original_thread = threading.Thread

    class Thread(original_thread):
        def join(self, timeout=None):
            if self.name == "avelis-webhooks":
                joins.append(timeout)
                delivery_threads.append(self)
                # 验证传入原生 join 的硬期限，不让测试本身等待五秒。
                return super().join(timeout=min(timeout, 0.01) if timeout is not None else None)
            return super().join(timeout)

    def deliver():
        started.set()
        release.wait(2)

    def scan():
        assert started.wait(1)
        worker.handlers[jobs.signal.SIGINT]()

    monkeypatch.setattr(jobs, "threading", SimpleNamespace(Thread=Thread, Event=threading.Event))
    monkeypatch.setattr(jobs, "scan_once", scan)
    monkeypatch.setattr(delivery, "deliver_due", deliver)
    try:
        jobs.main()
        assert joins == [5.0]
        assert delivery_threads[0].is_alive()
    finally:
        release.set()
        if delivery_threads:
            original_thread.join(delivery_threads[0], timeout=1)
