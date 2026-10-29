"""采集与调度的并发边界；只检查 SQL 和内存状态，不连接数据库或 Redis。"""

from __future__ import annotations

import operator
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from avelis import db, jobs
from avelis.cache import LockBusy
from avelis.models import CollectionRun, Datasource, Job


@pytest.fixture(autouse=True)
def no_external_connections(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("单元测试不得访问数据库或 Redis")

    monkeypatch.setattr(db, "get_engine", forbidden)
    monkeypatch.setattr(jobs, "session_scope", forbidden)
    monkeypatch.setattr(jobs.cache, "client", forbidden)
    monkeypatch.setattr(jobs.cache, "lock", forbidden)


@pytest.fixture
def lock_connection(monkeypatch):
    connection = MagicMock()
    connection.execution_options.return_value = connection
    connection.scalar.side_effect = [True, True]
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = connection
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    return engine, connection


def test_advisory_lock_holds_dedicated_connection_until_exit(lock_connection):
    engine, connection = lock_connection
    with db.advisory_lock("collect:source"):
        engine.connect.assert_called_once_with()
        connection.execution_options.assert_called_once_with(isolation_level="AUTOCOMMIT")
        assert connection.scalar.call_count == 1
        engine.connect.return_value.__exit__.assert_not_called()
    calls = connection.scalar.call_args_list
    assert str(calls[0].args[0]) == "SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"
    assert str(calls[1].args[0]) == "SELECT pg_advisory_unlock(hashtextextended(:key, 0))"
    assert [call.args[1] for call in calls] == [{"key": "collect:source"}] * 2
    engine.connect.return_value.__exit__.assert_called_once()
    connection.invalidate.assert_not_called()


def test_advisory_lock_busy_does_not_run_body_or_unlock(lock_connection):
    engine, connection = lock_connection
    connection.scalar.side_effect = [False]
    with pytest.raises(LockBusy):
        with db.advisory_lock("collect:source"):
            pytest.fail("未获得锁时不能执行采集")
    assert connection.scalar.call_count == 1
    engine.connect.return_value.__exit__.assert_called_once()


def test_advisory_lock_unlocks_when_collection_raises(lock_connection):
    _, connection = lock_connection
    with pytest.raises(ValueError, match="采集失败"):
        with db.advisory_lock("collect:source"):
            raise ValueError("采集失败")
    assert connection.scalar.call_count == 2
    assert "pg_advisory_unlock" in str(connection.scalar.call_args.args[0])
    connection.invalidate.assert_not_called()


def test_advisory_lock_discards_connection_when_unlock_fails(lock_connection):
    engine, connection = lock_connection
    connection.scalar.side_effect = [True, RuntimeError("解锁连接已断开")]
    with pytest.raises(RuntimeError, match="解锁连接已断开"):
        with db.advisory_lock("collect:source"):
            pass
    connection.invalidate.assert_called_once_with()
    engine.connect.return_value.__exit__.assert_called_once()


@pytest.mark.parametrize("fails", [False, True])
def test_collect_now_uses_postgres_lock_without_redis(monkeypatch, fails):
    events = []

    @contextmanager
    def lock(key):
        events.append(("lock", key))
        try:
            yield
        finally:
            events.append(("unlock", key))

    def collect(datasource_id, **kwargs):
        events.append(("collect", datasource_id, kwargs))
        if fails:
            raise ValueError("采集失败")
        return "run-id"

    monkeypatch.setattr(jobs, "advisory_lock", lock)
    monkeypatch.setattr(jobs, "run_collection", collect)
    if fails:
        with pytest.raises(ValueError, match="采集失败"):
            jobs.collect_now("source", trigger="manual", actor_id="actor")
    else:
        assert jobs.collect_now("source", trigger="manual", actor_id="actor") == "run-id"
    assert events == [
        ("lock", "collect:source"),
        ("collect", "source", {"trigger": "manual", "actor_id": "actor"}),
        ("unlock", "collect:source"),
    ]


class JobSession:
    """按实际 UPDATE 的 WHERE 条件更新内存任务，缺少终态围栏时测试会失败。"""

    def __init__(self, job):
        self.job = job
        self.statements = []

    def apply_update(self, statement):
        self.statements.append(statement)
        compiled = statement.compile()
        for predicate in statement._where_criteria:
            assert predicate.operator in (operator.eq, operator.lt)
            if not predicate.operator(getattr(self.job, predicate.left.name), predicate.right.value):
                return 0
        for field in ("status", "started_at", "finished_at", "error", "result"):
            if field in compiled.params:
                setattr(self.job, field, compiled.params[field])
        if "attempts=" in str(compiled).split(" WHERE ")[0]:
            self.job.attempts += compiled.params["attempts_1"]
        return 1

    def execute(self, statement):
        return SimpleNamespace(rowcount=self.apply_update(statement))

    def scalars(self, statement):
        return [self.job.id] if self.apply_update(statement) else []

    def get(self, kind, job_id):
        assert kind is Job and job_id == self.job.id
        return self.job


@pytest.fixture
def job_session(monkeypatch):
    now = [datetime(2026, 10, 29, 12)]
    job = Job(id="job-id", kind="collect", payload={"datasource_id": "source"}, created_by="actor",
              status="queued", attempts=3, result={}, error=None)
    session = JobSession(job)

    @contextmanager
    def scope():
        yield session

    monkeypatch.setattr(jobs, "session_scope", scope)
    monkeypatch.setattr(jobs, "utcnow", lambda: now[0])
    return session, now


def test_run_job_claims_and_finishes_only_its_running_attempt(monkeypatch, job_session):
    session, _ = job_session

    def collect(datasource_id, **kwargs):
        assert session.job.status == "running" and session.job.attempts == 4
        assert datasource_id == "source" and kwargs == {"trigger": "manual", "actor_id": "actor"}
        return "run-id"

    monkeypatch.setattr(jobs, "collect_now", collect)
    jobs.run_job("job-id")
    assert session.job.status == "succeeded" and session.job.result == {"run_id": "run-id"}
    terminal = session.statements[-1].compile(compile_kwargs={"literal_binds": False})
    assert "job.status =" in str(terminal) and "job.attempts =" in str(terminal)
    assert terminal.params["status_1"] == "running" and terminal.params["attempts_1"] == 4


def test_run_job_does_not_execute_unclaimed_job(monkeypatch, job_session):
    session, _ = job_session
    session.job.status = "running"
    monkeypatch.setattr(jobs, "collect_now", lambda *args, **kwargs: pytest.fail("不得执行其他 worker 的任务"))
    jobs.run_job("job-id")
    assert len(session.statements) == 1
    assert session.job.attempts == 3 and session.job.status == "running"


@pytest.mark.parametrize("new_state", ["abandoned", "reclaimed", "finished"])
@pytest.mark.parametrize("collection_fails", [False, True])
def test_slow_worker_cannot_overwrite_abandoned_or_newer_attempt(
    monkeypatch, job_session, new_state, collection_fails,
):
    session, now = job_session
    protected = {}

    def collect(*args, **kwargs):
        if new_state == "abandoned":
            now[0] += timedelta(seconds=60)
            assert jobs.abandoned_jobs(older_than_seconds=30) == ["job-id"]
        elif new_state == "reclaimed":
            session.job.attempts += 1
            session.job.result = {"new_attempt": "pending"}
        else:
            session.job.status = "succeeded"
            session.job.result = {"new_attempt": "finished"}
        protected.update({field: getattr(session.job, field)
                          for field in ("status", "attempts", "result", "error")})
        if collection_fails:
            raise ValueError("旧 worker 的迟到失败")
        return "late-run"

    monkeypatch.setattr(jobs, "collect_now", collect)
    jobs.run_job("job-id")
    assert {field: getattr(session.job, field) for field in protected} == protected
    assert session.job.result != {"run_id": "late-run"}


def test_abandoned_jobs_uses_single_conditional_update(job_session):
    session, now = job_session
    session.job.status = "running"
    session.job.started_at = now[0] - timedelta(seconds=60)
    assert jobs.abandoned_jobs(older_than_seconds=30) == ["job-id"]
    assert len(session.statements) == 1 and session.job.status == "failed"
    compiled = session.statements[0].compile()
    assert str(compiled).startswith("UPDATE job SET ") and "RETURNING job.id" in str(compiled)
    assert compiled.params["status_1"] == "running"
    assert compiled.params["started_at_1"] == now[0] - timedelta(seconds=30)
    assert jobs.abandoned_jobs(older_than_seconds=30) == []


class ScheduleSession:
    def __init__(self, events, states):
        self.events = events
        self.states = states
        self.created = []
        self.current = None

    def scalars(self, statement):
        assert statement.column_descriptions[0]["entity"] is Datasource
        assert "ORDER BY datasource.id" in str(statement)
        self.events.append("sources")
        return [Datasource(id=key, collect_interval_minutes=15) for key in self.states]

    def execute(self, statement, params):
        assert str(statement) == "SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"
        self.current = params["key"].removeprefix("schedule:collect:")
        assert params["key"] == f"schedule:collect:{self.current}"
        self.events.append(f"lock:{self.current}")

    def scalar(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        assert entity in (CollectionRun, Job)
        field = "last" if entity is CollectionRun else "pending"
        self.events.append(f"{field}:{self.current}")
        return self.states[self.current][field]

    def add(self, job):
        assert isinstance(job, Job) and job.payload["datasource_id"] == self.current
        self.events.append(f"add:{self.current}")
        self.created.append(job)

    def flush(self):
        self.created[-1].id = f"job-{len(self.created)}"
        self.events.append(f"flush:{self.current}")


@pytest.mark.parametrize("redis_fails", [False, True])
def test_schedule_locks_before_pending_check_and_wakes_only_after_commit(monkeypatch, redis_fails):
    now = datetime(2026, 10, 29, 12)
    events = []
    session = ScheduleSession(events, {
        "due": {"last": None, "pending": None},
        "pending": {"last": None, "pending": "existing-job"},
        "recent": {"last": now - timedelta(minutes=1), "pending": None},
    })

    @contextmanager
    def scope():
        events.append("begin")
        yield session
        events.append("commit")

    def lpush(queue, job_id):
        assert queue == jobs.cache.JOB_QUEUE and job_id == "job-1"
        events.append("redis")
        if redis_fails:
            raise ConnectionError("Redis 不可用")

    monkeypatch.setattr(jobs, "session_scope", scope)
    monkeypatch.setattr(jobs, "utcnow", lambda: now)
    monkeypatch.setattr(jobs.cache, "client", lambda: SimpleNamespace(lpush=lpush))
    monkeypatch.setattr(jobs, "enqueue", lambda *args, **kwargs: pytest.fail("调度不得另开事务入队"))
    assert jobs.schedule_due() == ["job-1"]
    assert events == [
        "begin", "sources", "lock:due", "last:due", "pending:due", "add:due", "flush:due",
        "lock:pending", "last:pending", "pending:pending", "lock:recent", "last:recent", "pending:recent",
        "commit", "redis",
    ]
    assert len(session.created) == 1
    assert session.created[0].status == "queued"
    assert session.created[0].payload == {"datasource_id": "due", "trigger": "schedule"}


def test_enqueue_commits_durable_job_before_redis_failure(monkeypatch):
    events = []
    session = ScheduleSession(events, {"source": {"last": None, "pending": None}})
    session.current = "source"

    @contextmanager
    def scope():
        yield session
        events.append("commit")

    def client():
        events.append("redis")
        raise ConnectionError("Redis 不可用")

    monkeypatch.setattr(jobs, "session_scope", scope)
    monkeypatch.setattr(jobs.cache, "client", client)
    assert jobs.enqueue("collect", {"datasource_id": "source"}, "actor") == "job-1"
    assert events == ["add:source", "flush:source", "commit", "redis"]
    assert session.created[0].status == "queued" and session.created[0].created_by == "actor"
