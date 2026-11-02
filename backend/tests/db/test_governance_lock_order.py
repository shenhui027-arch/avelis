"""采集收尾与超时回收使用真实 PostgreSQL 连接竞争，且只访问 meta_db 独立测试库。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, local
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from avelis import events, jobs
from avelis.collect import service as collect
from avelis.connectors.base import ObservedAsset, ObservedColumn, Snapshot
from avelis.connectors.ddl import AuditUnavailable
from avelis.db import get_engine, new_id, session_scope, utcnow
from avelis.importer import batch as importer
from avelis.models import (
    Asset,
    AuditEvent,
    CollectionRun,
    Datasource,
    EventOutbox,
    ImportBatch,
    Job,
    WebhookSubscription,
)

pytestmark = pytest.mark.db


def test_failed_run_and_event_commit_before_guard_and_reclaim(factory, monkeypatch):
    actor_id = factory.user()
    with session_scope() as session:
        ds = Datasource(name=f"failure-gap-{new_id()}", kind="postgres", host="offline.invalid", port=1)
        session.add(ds)
        session.flush()
        datasource_id = ds.id
        job_id = jobs._enqueue(session, "collect", {"datasource_id": ds.id}, actor_id)
        subscription = WebhookSubscription(
            name=f"failure-gap-{new_id()}",
            url="https://example.invalid/hook",
            event_types=["collect.failed"],
            enabled=True,
            secret_enc="unused-offline-fixture",
            created_by=actor_id,
        )
        session.add(subscription)
        session.flush()
        subscription_id = subscription.id

    def snapshot():
        raise RuntimeError("offline snapshot failure")

    monkeypatch.setattr(collect, "collect_connector", lambda *_: SimpleNamespace(snapshot=snapshot))
    run_collection = collect._run_collection
    committed_counts = []

    def check_before_guard(*args, **kwargs):
        try:
            return run_collection(*args, **kwargs)
        except collect.CollectionFailed as exc:
            # guard 尚未见到异常时，failed 状态、事件与发件箱已同时提交。
            assert exc.failure_event_recorded is True
            with session_scope() as checking:
                job = checking.get(Job, job_id)
                assert job.status == "running"
                run = checking.get(CollectionRun, job.result["run_id"])
                assert run.status == "failed" and run.finished_at is not None
                audits = list(
                    checking.scalars(
                        select(AuditEvent).where(
                            AuditEvent.object_id == datasource_id, AuditEvent.action == "collect.failed"
                        )
                    )
                )
                outbox = list(
                    checking.scalars(select(EventOutbox).where(EventOutbox.subscription_id == subscription_id))
                )
                committed_counts.append((len(audits), len(outbox)))
                assert outbox[0].event_id == audits[0].id
                job.started_at = utcnow() - timedelta(minutes=5)
            assert job_id in jobs.abandoned_jobs(older_than_seconds=60)
            raise

    monkeypatch.setattr(collect, "_run_collection", check_before_guard)
    jobs.run_job(job_id)
    assert committed_counts == [(1, 1)]
    assert job_id not in jobs.abandoned_jobs(older_than_seconds=60)
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job.status == "failed" and job.finished_at is not None
        assert job.result["abandoned_governance"] == "done"
        run = session.get(CollectionRun, job.result["run_id"])
        assert run.status == "failed" and run.error == "RuntimeError: offline snapshot failure"
        audits = list(
            session.scalars(
                select(AuditEvent).where(AuditEvent.object_id == datasource_id, AuditEvent.action == "collect.failed")
            )
        )
        assert len(audits) == 1
        failed = events.event_from_audit(audits[0])
        assert failed.type == "collect.failed" and failed.error.code == "CollectionFailed"
        outbox = list(session.scalars(select(EventOutbox).where(EventOutbox.subscription_id == subscription_id)))
        assert len(outbox) == 1 and outbox[0].event_id == audits[0].id


def test_reconcile_between_reclaim_transactions_emits_once(factory, monkeypatch):
    actor_id = factory.user()
    with session_scope() as session:
        ds = Datasource(name=f"reconcile-gap-{new_id()}", kind="postgres", host="offline.invalid", port=1)
        session.add(ds)
        session.flush()
        imported = ImportBatch(datasource_id=ds.id, status="queued", created_by=actor_id)
        session.add(imported)
        session.flush()
        batch_id = imported.id
        job_id = jobs._enqueue(session, "import", {"batch_id": batch_id}, actor_id)
        imported.job_id = job_id
        execution = session.get(Job, job_id)
        execution.status, execution.attempts = "running", 1
        execution.started_at = utcnow() - timedelta(minutes=5)
        subscription = WebhookSubscription(
            name=f"reconcile-gap-{new_id()}",
            url="https://example.invalid/hook",
            event_types=["lineage_import.failed"],
            enabled=True,
            secret_enc="unused-offline-fixture",
            created_by=actor_id,
        )
        session.add(subscription)
        session.flush()
        subscription_id = subscription.id

    record_abandoned = jobs._record_abandoned_governance
    reconciled = []

    def reconcile_before_governance(current_job_id, *args):
        if current_job_id == job_id:
            with session_scope() as session:
                execution = session.get(Job, job_id)
                assert execution.status == "failed"
                assert execution.result["abandoned_governance"] == "pending"
                imported = session.get(ImportBatch, batch_id)
                assert imported.status == "queued"
                assert not list(session.scalars(select(AuditEvent).where(AuditEvent.object_id == batch_id)))
                importer.reconcile_batch(session, imported)
                assert imported.status == "failed"
            # 与 GET 一样提交调用方事务；重复读取不能再生成一次失败。
            with session_scope() as session:
                importer.reconcile_batch(session, session.get(ImportBatch, batch_id))
                audits = list(session.scalars(select(AuditEvent).where(AuditEvent.object_id == batch_id)))
                outbox = list(
                    session.scalars(select(EventOutbox).where(EventOutbox.subscription_id == subscription_id))
                )
                assert len(audits) == len(outbox) == 1
                assert outbox[0].event_id == audits[0].id
                reconciled.append(audits[0].id)
        return record_abandoned(current_job_id, *args)

    monkeypatch.setattr(jobs, "_record_abandoned_governance", reconcile_before_governance)
    assert job_id in jobs.abandoned_jobs(older_than_seconds=60)
    assert job_id not in jobs.abandoned_jobs(older_than_seconds=60)
    with session_scope() as session:
        imported = session.get(ImportBatch, batch_id)
        execution = session.get(Job, job_id)
        assert imported.status == execution.status == "failed"
        assert execution.result["abandoned_governance"] == "done"
        audits = list(session.scalars(select(AuditEvent).where(AuditEvent.object_id == batch_id)))
        assert [row.id for row in audits] == reconciled
        failed = events.event_from_audit(audits[0])
        assert failed.type == "lineage_import.failed" and failed.error.code == "WORKER_FAILED"
        assert failed.actor.id == actor_id and failed.detail["job_id"] == job_id
        outbox = list(session.scalars(select(EventOutbox).where(EventOutbox.subscription_id == subscription_id)))
        assert len(outbox) == 1 and outbox[0].event_id == audits[0].id


@pytest.mark.parametrize("kind", ["collect", "import"])
@pytest.mark.parametrize("winner", ["worker", "reclaim"])
def test_normal_failure_and_reclaimer_lock_job_first_and_emit_once(factory, monkeypatch, caplog, kind, winner):
    actor_id = factory.user()
    event_type = "collect.failed" if kind == "collect" else "lineage_import.failed"
    with session_scope() as session:
        ds = Datasource(name=f"failure-lock-{new_id()}", kind="postgres", host="offline.invalid", port=1)
        session.add(ds)
        session.flush()
        object_id = ds.id
        if kind == "import":
            imported = ImportBatch(datasource_id=ds.id, status="queued", created_by=actor_id)
            session.add(imported)
            session.flush()
            object_id = imported.id
        payload = {"datasource_id": ds.id} if kind == "collect" else {"batch_id": object_id}
        job_id = jobs._enqueue(session, kind, payload, actor_id)
        if kind == "import":
            imported.job_id = job_id
        subscription = WebhookSubscription(
            name=f"failure-lock-{new_id()}",
            url="https://example.invalid/hook",
            event_types=[event_type],
            enabled=True,
            secret_enc="unused-offline-fixture",
            created_by=actor_id,
        )
        session.add(subscription)
        session.flush()
        subscription_id = subscription.id

    failure_ready, worker_locked, worker_attempt, reclaim_locked, reclaim_attempt = (Event() for _ in range(5))
    reclaim_committed = Event()
    thread = local()
    engine = get_engine()
    failure_writers = []

    def before_statement(_conn, _cursor, statement, _params, _context, _many):
        role = getattr(thread, "role", None)
        if role == "worker" and getattr(thread, "failing", False):
            if "FROM job" in statement and "FOR UPDATE" in statement:
                worker_attempt.set()
        if role == "reclaim" and statement.startswith("UPDATE job SET"):
            reclaim_attempt.set()
        if role == "reclaim" or (role == "worker" and getattr(thread, "failing", False)):
            if statement.startswith(("UPDATE collection_run SET", "UPDATE import_batch SET")):
                assert getattr(thread, "locked_job", False), "必须先锁 Job 再修改失败对象"
                failure_writers.append(role)

    def after_statement(_conn, _cursor, statement, _params, _context, _many):
        role = getattr(thread, "role", None)
        if role == "worker" and getattr(thread, "failing", False):
            if "FROM job" in statement and "FOR UPDATE" in statement:
                thread.locked_job = True
                worker_locked.set()
                if winner == "worker":
                    assert reclaim_attempt.wait(10), "回收方未尝试更新 Job"
        if role == "reclaim" and statement.startswith("UPDATE job SET"):
            thread.locked_job = True
            reclaim_locked.set()
            if winner == "reclaim":
                assert worker_attempt.wait(10), "正常失败未尝试锁 Job"

    def fail_work(*_args, **_kwargs):
        # 此时初始运行事务已提交，快照/解析未持有 Job 或对象写锁。
        with session_scope() as session:
            session.get(Job, job_id).started_at = utcnow() - timedelta(minutes=5)
        thread.failing, thread.locked_job = True, False
        failure_ready.set()
        if winner == "reclaim":
            assert reclaim_locked.wait(10), "回收方未取得 Job 写锁"
        raise RuntimeError("offline worker failure")

    def run_worker():
        thread.role = "worker"
        jobs.run_job(job_id)

    def run_reclaim():
        thread.role = "reclaim"
        assert failure_ready.wait(10), "worker 未进入失败阶段"
        if winner == "worker":
            assert worker_locked.wait(10), "正常失败未取得 Job 锁"
        return jobs.abandoned_jobs(older_than_seconds=60)

    if winner == "worker":
        run_operation = collect._run_collection if kind == "collect" else importer._run_import
        record_abandoned = jobs._record_abandoned_governance

        def wait_before_job_finishes(*args, **kwargs):
            try:
                return run_operation(*args, **kwargs)
            except Exception as exc:
                assert exc.failure_event_recorded is True
                # 正常失败事务已提交；让回收首事务完成后再交给 run_job 终结 Job。
                assert reclaim_committed.wait(10), "回收首事务未提交"
                raise

        def confirm_reclaim_commit(current_job_id, *args):
            if current_job_id == job_id:
                # abandoned_jobs 已退出首事务，独立连接也必须看到 pending 终态。
                with session_scope() as session:
                    execution = session.get(Job, job_id)
                    assert execution.status == "failed"
                    assert execution.result["abandoned_governance"] == "pending"
                reclaim_committed.set()
            return record_abandoned(current_job_id, *args)

        module, name = (collect, "_run_collection") if kind == "collect" else (importer, "_run_import")
        monkeypatch.setattr(module, name, wait_before_job_finishes)
        monkeypatch.setattr(jobs, "_record_abandoned_governance", confirm_reclaim_commit)

    if kind == "collect":
        monkeypatch.setattr(collect, "collect_connector", lambda *_: SimpleNamespace(snapshot=fail_work))
    else:
        monkeypatch.setattr(importer, "_prepare", fail_work)
    event.listen(engine, "before_cursor_execute", before_statement)
    event.listen(engine, "after_cursor_execute", after_statement)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            worker = executor.submit(run_worker)
            reclaimer = executor.submit(run_reclaim)
            worker.result(timeout=30)
            assert job_id in reclaimer.result(timeout=30)
    finally:
        event.remove(engine, "before_cursor_execute", before_statement)
        event.remove(engine, "after_cursor_execute", after_statement)

    assert failure_writers == [winner]
    assert "deadlock detected" not in caplog.text.lower()
    assert job_id not in jobs.abandoned_jobs(older_than_seconds=60)
    with session_scope() as session:
        execution = session.get(Job, job_id)
        assert execution.status == "failed" and execution.finished_at is not None
        assert execution.result["abandoned_governance"] == "done"
        if kind == "collect":
            failed_object = session.get(CollectionRun, execution.result["run_id"])
            assert failed_object.finished_at is not None
        else:
            failed_object = session.get(ImportBatch, object_id)
            assert failed_object.progress["phase"] == "failed"
        assert failed_object.status == "failed"
        if winner == "worker":
            assert failed_object.error == "RuntimeError: offline worker failure"
        audits = list(
            session.scalars(
                select(AuditEvent).where(AuditEvent.object_id == object_id, AuditEvent.action == event_type)
            )
        )
        assert len(audits) == 1
        failed = events.event_from_audit(audits[0])
        assert failed.detail["job_id"] == job_id
        expected_code = (
            "WORKER_ABANDONED" if winner == "reclaim" else "CollectionFailed" if kind == "collect" else "RuntimeError"
        )
        assert failed.error.code == expected_code
        outbox = list(session.scalars(select(EventOutbox).where(EventOutbox.subscription_id == subscription_id)))
        assert len(outbox) == 1 and outbox[0].event_id == audits[0].id


@pytest.mark.parametrize("winner", ["collect", "reclaim"])
def test_collection_and_reclaimer_lock_job_first_without_deadlock(factory, monkeypatch, caplog, winner):
    actor_id = factory.user()
    with session_scope() as session:
        ds = Datasource(name=f"lock-{new_id()}", kind="postgres", host="offline.invalid", port=1)
        session.add(ds)
        session.flush()
        datasource_id = ds.id
        job_id = jobs._enqueue(session, "collect", {"datasource_id": ds.id, "trigger": "manual"}, actor_id)
        subscription = WebhookSubscription(
            name=f"lock-{new_id()}",
            url="https://example.invalid/hook",
            event_types=["collect.failed", "collect.completed"],
            enabled=True,
            secret_enc="unused-offline-fixture",
            created_by=actor_id,
        )
        session.add(subscription)
        session.flush()
        subscription_id = subscription.id

    snapshot_ready, reclaim_locked, collect_lock_attempt, reclaim_update = (Event() for _ in range(4))
    thread = local()
    engine = get_engine()
    apply = collect._apply_snapshot

    def before_statement(_conn, _cursor, statement, _params, _context, _many):
        role = getattr(thread, "role", None)
        if role == "collect" and getattr(thread, "finishing", False):
            if "FROM job" in statement and "FOR UPDATE" in statement:
                collect_lock_attempt.set()
        if role == "reclaim" and statement.startswith("UPDATE job SET"):
            reclaim_update.set()

    def after_statement(_conn, _cursor, statement, _params, _context, _many):
        if winner == "reclaim" and getattr(thread, "role", None) == "reclaim":
            if "FROM job" in statement and "FOR UPDATE" in statement:
                reclaim_locked.set()  # 回收方持有 Job，等采集尝试取得同一把锁。
                assert collect_lock_attempt.wait(10), "采集未尝试锁 Job"

    def snapshot():
        thread.finishing = True
        snapshot_ready.set()
        if winner == "reclaim":
            assert reclaim_locked.wait(10), "回收方未锁 Job"
        return Snapshot(
            server_identity={"instance_id": "lock-fixture"},
            assets=[
                ObservedAsset(
                    catalog="default_catalog",
                    db="public",
                    name="records",
                    kind="table",
                    physical_id="table-1",
                    columns=[ObservedColumn("id", "INT", 1)],
                )
            ],
        )

    def ddl_events(*_args, **_kwargs):
        raise AuditUnavailable("offline fixture", cause="not_enabled")

    def apply_snapshot(*args):
        if winner == "collect":
            assert reclaim_update.wait(10), "回收方未尝试更新 Job"
        return apply(*args)

    def run_collect():
        thread.role = "collect"
        jobs.run_job(job_id)

    def run_reclaim():
        thread.role = "reclaim"
        assert snapshot_ready.wait(10), "采集未读取快照"
        with session_scope() as session:
            session.get(Job, job_id).started_at = utcnow() - timedelta(minutes=5)
        return jobs.abandoned_jobs(older_than_seconds=60)

    monkeypatch.setattr(
        collect, "collect_connector", lambda *_: SimpleNamespace(snapshot=snapshot, ddl_events=ddl_events)
    )
    monkeypatch.setattr(collect, "_apply_snapshot", apply_snapshot)
    event.listen(engine, "before_cursor_execute", before_statement)
    event.listen(engine, "after_cursor_execute", after_statement)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            collector = executor.submit(run_collect)
            reclaimer = executor.submit(run_reclaim)
            collector.result(timeout=30)
            reclaimer.result(timeout=30)
    finally:
        event.remove(engine, "before_cursor_execute", before_statement)
        event.remove(engine, "after_cursor_execute", after_statement)

    # 再次扫描也不能重复终态审计或发件箱记录。
    jobs.abandoned_jobs(older_than_seconds=60)
    assert "deadlock detected" not in caplog.text.lower()
    with session_scope() as session:
        job = session.get(Job, job_id)
        runs = list(session.scalars(select(CollectionRun).where(CollectionRun.datasource_id == datasource_id)))
        assert len(runs) == 1
        expected_status = "succeeded" if winner == "collect" else "failed"
        assert job.status == runs[0].status == expected_status
        assert runs[0].finished_at is not None
        if winner == "reclaim":
            assert job.result["abandoned_governance"] == "done"
        assets = list(session.scalars(select(Asset).where(Asset.datasource_id == datasource_id)))
        assert len(assets) == (1 if winner == "collect" else 0)
        audits = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.object_id == datasource_id, AuditEvent.action.in_(("collect", "collect.failed"))
                )
            )
        )
        assert len(audits) == 1
        emitted = events.event_from_audit(audits[0])
        assert emitted.type == ("collect.completed" if winner == "collect" else "collect.failed")
        if winner == "reclaim":
            assert emitted.error.code == "WORKER_ABANDONED"
        outbox = list(session.scalars(select(EventOutbox).where(EventOutbox.subscription_id == subscription_id)))
        assert len(outbox) == 1 and outbox[0].event_id == audits[0].id
