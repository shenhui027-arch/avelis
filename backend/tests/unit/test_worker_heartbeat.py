"""worker 心跳与退出清理：替换 Redis 和任务执行，不连接外部服务。"""

import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from avelis import cache, jobs


@pytest.fixture
def worker(monkeypatch):
    handlers = {}
    connection = MagicMock()
    connection.brpop.return_value = (cache.JOB_QUEUE, "job-id")
    scan = MagicMock()
    run = MagicMock()
    monkeypatch.setattr(jobs, "get_settings", lambda: SimpleNamespace(worker_schedule_seconds=60))
    monkeypatch.setattr(jobs.signal, "signal", lambda signum, handler: handlers.__setitem__(signum, handler))
    monkeypatch.setattr(jobs.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(jobs.time, "sleep", MagicMock())
    monkeypatch.setattr(jobs, "scan_once", scan)
    monkeypatch.setattr(jobs, "_deliver_events_loop", lambda stop: stop.wait())
    monkeypatch.setattr(jobs, "run_job", run)
    monkeypatch.setattr(cache, "client", lambda: connection)
    return SimpleNamespace(connection=connection, handlers=handlers, scan=scan, run=run)


@pytest.fixture
def heartbeat(monkeypatch):
    ticks = threading.Event()
    threads = []
    waits = []
    actions = []

    class StopEvent(threading.Event):
        def wait(self, timeout=None):
            if timeout == 20:
                waits.append(timeout)
                ticks.wait()  # 测试主动推进一次续写，停止信号也会唤醒等待。
                ticks.clear()
                return self.is_set()
            return super().wait(timeout)

        def set(self):
            actions.append("stop")
            super().set()
            ticks.set()

    class HeartbeatThread(threading.Thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            threads.append(self)

        def join(self, timeout=None):
            actions.append(("join", timeout))
            return super().join(timeout)

    stop = StopEvent()
    events = iter([stop, threading.Event(), threading.Event()])

    def thread(*args, **kwargs):
        if kwargs.get("name") == "avelis-webhooks":
            return threading.Thread(*args, **kwargs)
        return HeartbeatThread(*args, **kwargs)

    # 只替换 worker 引用，保留真实 Thread 内部使用的 threading.Event。
    monkeypatch.setattr(jobs, "threading", SimpleNamespace(Thread=thread, Event=lambda: next(events)))
    return SimpleNamespace(tick=ticks, threads=threads, waits=waits, actions=actions, stop=stop)


def test_worker_starts_daemon_heartbeat_before_scan_without_writing_each_loop(monkeypatch, worker, heartbeat):
    now = datetime(2026, 11, 1, 8)
    monkeypatch.setattr(jobs, "utcnow", lambda: now)
    writers = []
    worker.connection.set.side_effect = lambda *args, **kwargs: writers.append(threading.current_thread())
    worker.connection.delete.side_effect = lambda *args: heartbeat.actions.append("delete")

    def scan():
        assert worker.connection.set.call_count == 1  # 启动即写入，先于可能耗时的扫描
        assert writers == heartbeat.threads
        assert writers[0].daemon

    def pop(*args, **kwargs):
        count = worker.connection.brpop.call_count
        if count >= 2:
            worker.handlers[jobs.signal.SIGINT]()
        return cache.JOB_QUEUE, "job-id"

    worker.scan.side_effect = scan
    worker.connection.brpop.side_effect = pop
    jobs.main()

    worker.connection.set.assert_called_once_with(
        cache.WORKER_HEARTBEAT, now.replace(tzinfo=timezone.utc).isoformat(), ex=60
    )
    assert worker.connection.brpop.call_args_list == [call([cache.JOB_QUEUE], timeout=5)] * 2
    assert worker.run.call_args_list == [call("job-id")] * 2
    assert heartbeat.waits == [20]
    assert heartbeat.stop.is_set()
    assert not heartbeat.threads[0].is_alive()
    assert heartbeat.actions == ["stop", ("join", 1), "delete"]
    worker.connection.delete.assert_called_once_with(cache.WORKER_HEARTBEAT)
    assert worker.connection.mock_calls[-1] == call.delete(cache.WORKER_HEARTBEAT)


def test_worker_renews_heartbeat_while_job_is_blocked(monkeypatch, worker, heartbeat):
    now = datetime(2026, 11, 1, 8)
    timestamps = iter([now, now + timedelta(seconds=20)])
    monkeypatch.setattr(jobs, "utcnow", lambda: next(timestamps))
    running = threading.Event()
    renewed = threading.Event()
    writes_during_job = []
    writers = []

    def write(*args, **kwargs):
        writers.append(threading.current_thread())
        writes_during_job.append(running.is_set())
        if len(writes_during_job) == 2:
            renewed.set()

    def run(job_id):
        running.set()
        heartbeat.tick.set()
        try:
            assert renewed.wait(timeout=2), "run_job 阻塞期间心跳没有续写"
        finally:
            running.clear()
            worker.handlers[jobs.signal.SIGINT]()

    worker.connection.set.side_effect = write
    worker.run.side_effect = run
    jobs.main()

    assert writes_during_job == [False, True]
    assert writers == heartbeat.threads * 2
    assert worker.connection.set.call_args_list == [
        call(cache.WORKER_HEARTBEAT, now.replace(tzinfo=timezone.utc).isoformat(), ex=60),
        call(cache.WORKER_HEARTBEAT, (now + timedelta(seconds=20)).replace(tzinfo=timezone.utc).isoformat(), ex=60),
    ]
    worker.run.assert_called_once_with("job-id")
    assert not heartbeat.threads[0].is_alive()
    worker.connection.delete.assert_called_once_with(cache.WORKER_HEARTBEAT)


@pytest.mark.parametrize("operation", ["set", "client"])
def test_worker_and_heartbeat_continue_after_heartbeat_redis_failure(monkeypatch, worker, heartbeat, caplog, operation):
    error = ConnectionError("Redis 不可用")
    retried = threading.Event()
    running = threading.Event()
    attempts = []
    attempts_during_job = []

    def fail():
        attempts.append(threading.current_thread())
        attempts_during_job.append(running.is_set())
        if len(attempts) == 2:
            retried.set()
        raise error

    if operation == "set":
        worker.connection.set.side_effect = lambda *args, **kwargs: fail()
    else:

        def client():
            if threading.current_thread() is heartbeat.threads[0]:
                return fail()
            return worker.connection

        monkeypatch.setattr(cache, "client", client)

    def run(job_id):
        running.set()
        heartbeat.tick.set()
        try:
            assert retried.wait(timeout=2), "Redis 写入故障后心跳线程没有重试"
        finally:
            running.clear()
            worker.handlers[jobs.signal.SIGINT]()

    worker.run.side_effect = run
    jobs.main()

    assert attempts == heartbeat.threads * 2
    assert attempts_during_job == [False, True]
    assert sum(record.getMessage() == "写入 worker 心跳 失败，下一轮重试" for record in caplog.records) == 2
    worker.scan.assert_called_once_with()
    worker.run.assert_called_once_with("job-id")
    assert not heartbeat.threads[0].is_alive()
    worker.connection.delete.assert_called_once_with(cache.WORKER_HEARTBEAT)


@pytest.mark.parametrize("operation", ["set", "brpop", "delete", "client"])
def test_worker_continues_when_redis_fails(monkeypatch, worker, heartbeat, operation):
    error = ConnectionError("Redis 不可用")
    if operation == "client":
        client = MagicMock(side_effect=error)
        monkeypatch.setattr(cache, "client", client)
    else:
        getattr(worker.connection, operation).side_effect = error
    worker.scan.side_effect = lambda: worker.handlers[jobs.signal.SIGINT]()

    jobs.main()

    worker.scan.assert_called_once_with()
    if operation in ("set", "delete"):
        worker.run.assert_called_once_with("job-id")
    else:
        worker.run.assert_not_called()
        jobs.time.sleep.assert_called_once_with(5)
    if operation == "client":
        assert client.call_count == 3  # 写心跳、等待任务、退出清理均失败也能正常退出
    else:
        worker.connection.delete.assert_called_once_with(cache.WORKER_HEARTBEAT)
    assert not heartbeat.threads[0].is_alive()
