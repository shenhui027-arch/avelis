"""失败兜底、协调与回收的真实事务回归；只使用 meta_db 独立测试库。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from threading import Event, local
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from avelis import events, jobs
from avelis.api.imports import list_imports
from avelis.collect import service as collect
from avelis.db import get_engine, new_id, session_scope, utcnow
from avelis.importer import batch as importer
from avelis.models import (
    AuditEvent,
    CollectionRun,
    Datasource,
    EventOutbox,
    ImportBatch,
    Job,
    User,
    WebhookSubscription,
)

pytestmark = pytest.mark.db


def _failure_world(factory, kind):
    actor_id = factory.user(("steward",))
    event_type = "collect.failed" if kind == "collect" else "lineage_import.failed"
    with session_scope() as session:
        source = Datasource(name=f"guard-recovery-{new_id()}", kind="postgres", host="offline.invalid", port=1)
        session.add(source)
        session.flush()
        object_id = source.id
        if kind == "import":
            imported = ImportBatch(datasource_id=source.id, status="queued", created_by=actor_id)
            session.add(imported)
            session.flush()
            object_id = imported.id
        payload = {"datasource_id": source.id} if kind == "collect" else {"batch_id": object_id}
        job_id = jobs._enqueue(session, kind, payload, actor_id)
        if kind == "import":
            imported.job_id = job_id
        subscription = WebhookSubscription(
            name=f"guard-recovery-{new_id()}",
            url="https://example.invalid/hook",
            event_types=[event_type],
            enabled=True,
            secret_enc="unused-offline-fixture",
            created_by=actor_id,
        )
        session.add(subscription)
        session.flush()
        return SimpleNamespace(
            actor_id=actor_id,
            datasource_id=source.id,
            object_id=object_id,
            event_type=event_type,
            job_id=job_id,
            subscription_id=subscription.id,
        )


def _failed_object(session, world, kind):
    if kind == "import":
        return session.get(ImportBatch, world.object_id)
    return session.get(CollectionRun, session.get(Job, world.job_id).result["run_id"])


def _assert_attempt_events(session, world, attempts):
    audits = list(
        session.scalars(
            select(AuditEvent).where(AuditEvent.object_id == world.object_id, AuditEvent.action == world.event_type)
        )
    )
    assert len(audits) == len(attempts)
    assert sorted(row.detail["attempt"] for row in audits) == attempts
    assert all(row.detail["job_id"] == world.job_id for row in audits)
    outbox = list(session.scalars(select(EventOutbox).where(EventOutbox.subscription_id == world.subscription_id)))
    assert len(outbox) == len(attempts)
    assert {row.event_id for row in outbox} == {row.id for row in audits}


@pytest.mark.parametrize("kind,followup", [("import", "reconcile"), ("import", "reclaim"), ("collect", "reclaim")])
@pytest.mark.parametrize("transition_fails", [False, True])
def test_rolled_back_failure_guard_and_later_recovery_emit_once_per_attempt(
    factory, monkeypatch, kind, followup, transition_fails
):
    world = _failure_world(factory, kind)
    original_record, original_guard = events.record_failure, events.failure_guard
    original_transition = events._fail_guarded_object
    rolled_back_attempts, guarded_attempts = [], []

    class FinishWriteRolledBack(Exception):
        pass

    def fail_work(*_args, **_kwargs):
        raise RuntimeError("offline worker failure")

    def roll_back_first_failure(session, actor, event_type, object_type, object_id=None, **detail):
        if object_id != world.object_id or event_type != world.event_type:
            return original_record(session, actor, event_type, object_type, object_id, **detail)
        attempt = session.get(Job, world.job_id).attempts
        row = original_record(session, actor, event_type, object_type, object_id, **detail)
        if attempt not in rolled_back_attempts:
            # 真实写入 failed、审计和发件箱后失败，迫使 session_scope 回滚整个收尾事务。
            session.flush()
            assert _failed_object(session, world, kind).status == "failed"
            rolled_back_attempts.append(attempt)
            raise FinishWriteRolledBack("offline finish transaction failure")
        return row

    def fail_transition_after_flush(session, *args):
        original_transition(session, *args)
        session.flush()
        # guard 必须回滚保存点中的对象变更，再在其外写下同一次尝试的失败事件。
        raise FinishWriteRolledBack("offline fallback object transition failure")

    @contextmanager
    def inspect_guard_commit(*args, **kwargs):
        try:
            with original_guard(*args, **kwargs):
                yield
        except Exception:
            # 此时兜底事务已独立提交，run_job 尚未提交 Job 的 failed 终态。
            with session_scope() as session:
                execution = session.get(Job, world.job_id)
                assert execution.status == "running"
                attempt = execution.attempts
                assert attempt in rolled_back_attempts
                assert _failed_object(session, world, kind).status == ("running" if transition_fails else "failed")
                _assert_attempt_events(session, world, list(range(1, attempt + 1)))
                guarded_attempts.append(attempt)
                if followup == "reclaim":
                    execution.started_at = utcnow() - timedelta(minutes=5)
            if followup == "reclaim":
                assert world.job_id in jobs.abandoned_jobs(older_than_seconds=60)
                with session_scope() as session:
                    assert _failed_object(session, world, kind).status == "failed"
                    _assert_attempt_events(session, world, list(range(1, attempt + 1)))
            raise

    monkeypatch.setattr(events, "record_failure", roll_back_first_failure)
    monkeypatch.setattr(events, "failure_guard", inspect_guard_commit)
    if transition_fails:
        monkeypatch.setattr(events, "_fail_guarded_object", fail_transition_after_flush)
    if kind == "collect":
        monkeypatch.setattr(collect, "collect_connector", lambda *_: SimpleNamespace(snapshot=fail_work))
    else:
        monkeypatch.setattr(importer, "_prepare", fail_work)

    # 同一个 Job 重试时，持久键只抑制同一次尝试，不能吞掉下一次尝试的失败。
    for attempt in (1, 2):
        if attempt == 2:
            with session_scope() as session:
                execution = session.get(Job, world.job_id)
                execution.status, execution.error, execution.finished_at = "queued", None, None
                if kind == "import":
                    imported = session.get(ImportBatch, world.object_id)
                    imported.status, imported.error, imported.progress = "queued", None, {}
        jobs.run_job(world.job_id)
        with session_scope() as session:
            execution = session.get(Job, world.job_id)
            assert execution.status == "failed" and execution.attempts == attempt
            if kind == "import":
                imported = session.get(ImportBatch, world.object_id)
                importer.reconcile_batch(session, imported)
                assert imported.status == "failed"
        # 调用方事务提交后，再读和再回收均不能重复审计或发件箱。
        if kind == "import":
            with session_scope() as session:
                importer.reconcile_batch(session, session.get(ImportBatch, world.object_id))
        assert world.job_id not in jobs.abandoned_jobs(older_than_seconds=60)
        with session_scope() as session:
            assert _failed_object(session, world, kind).status == "failed"
            _assert_attempt_events(session, world, list(range(1, attempt + 1)))
    assert rolled_back_attempts == guarded_attempts == [1, 2]


def _two_imports(factory, *, failed=False):
    actor_id = factory.user(("steward",))
    now = utcnow()
    with session_scope() as session:
        source = Datasource(name=f"list-reclaim-{new_id()}", kind="postgres", host="offline.invalid", port=1)
        session.add(source)
        session.flush()
        imports, job_ids = [], []
        for index, prefix in enumerate(("11", "ee")):
            job_id = prefix + new_id()[2:]
            imported = ImportBatch(
                datasource_id=source.id,
                status="running",
                created_by=actor_id,
                created_at=now + timedelta(seconds=index),
            )
            session.add(imported)
            session.flush()
            session.add(
                Job(
                    id=job_id,
                    kind="import",
                    payload={"batch_id": imported.id},
                    status="failed" if failed else "running",
                    attempts=1,
                    created_by=actor_id,
                    started_at=now - timedelta(minutes=5),
                    error="offline worker failure" if failed else None,
                )
            )
            session.flush()
            imported.job_id = job_id
            imports.append(imported.id)
            job_ids.append(job_id)
    return actor_id, imports, job_ids


def test_list_imports_and_two_expired_jobs_interleave_without_deadlock(factory, caplog):
    actor_id, import_ids, job_ids = _two_imports(factory)
    list_read, reclaim_locked, list_finished = (Event() for _ in range(3))
    thread = local()
    engine = get_engine()
    statements = []

    def after_statement(_conn, _cursor, statement, _params, _context, _many):
        role = getattr(thread, "role", None)
        if role == "list" and "FROM job" in statement and not getattr(thread, "read_seen", False):
            thread.read_seen = True
            statements.append(statement)
            list_read.set()
            assert reclaim_locked.wait(10), "回收方未完成有序 Job 加锁"
        if role == "reclaim" and "FROM job" in statement and "FOR UPDATE SKIP LOCKED" in statement:
            statements.append(statement)
            reclaim_locked.set()
            assert list_finished.wait(10), "列表请求在回收持锁时未能完成"

    def run_list():
        thread.role = "list"
        with session_scope() as session:
            result = list_imports(user=session.get(User, actor_id), session=session)
            list_finished.set()
            return result

    def run_reclaim():
        thread.role = "reclaim"
        assert list_read.wait(10), "列表未读取 Job 状态"
        return jobs.abandoned_jobs(older_than_seconds=60)

    event.listen(engine, "after_cursor_execute", after_statement)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            listing, reclaiming = executor.submit(run_list), executor.submit(run_reclaim)
            result = listing.result(timeout=30)
            assert set(job_ids).issubset(reclaiming.result(timeout=30))
    finally:
        event.remove(engine, "after_cursor_execute", after_statement)
    assert "FOR UPDATE" not in statements[0]
    assert "ORDER BY job.id" in statements[1]
    assert [row["id"] for row in result if row["id"] in import_ids] == list(reversed(import_ids))
    assert "deadlock detected" not in caplog.text.lower()
    with session_scope() as session:
        assert all(session.get(Job, job_id).status == "failed" for job_id in job_ids)
        assert all(session.get(ImportBatch, batch_id).status == "failed" for batch_id in import_ids)
        audits = list(session.scalars(select(AuditEvent).where(AuditEvent.object_id.in_(import_ids))))
        assert len(audits) == 2


def test_list_imports_locks_failed_jobs_in_id_order_and_keeps_newest_first(factory):
    actor_id, import_ids, job_ids = _two_imports(factory, failed=True)
    locked = []
    engine = get_engine()

    def after_statement(_conn, _cursor, statement, params, _context, _many):
        if "FROM job" in statement and "FOR UPDATE" in statement:
            locked.extend(value for value in params.values() if value in job_ids)

    event.listen(engine, "after_cursor_execute", after_statement)
    try:
        with session_scope() as session:
            result = list_imports(user=session.get(User, actor_id), session=session)
    finally:
        event.remove(engine, "after_cursor_execute", after_statement)
    assert locked == sorted(job_ids)
    assert [row["id"] for row in result if row["id"] in import_ids] == list(reversed(import_ids))
