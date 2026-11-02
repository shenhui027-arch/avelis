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
from avelis.models import Asset, AuditEvent, CollectionRun, Datasource, EventOutbox, Job, WebhookSubscription

pytestmark = pytest.mark.db


def test_reclaim_between_failed_run_commit_and_failure_guard_emits_once(factory, monkeypatch):
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
    record_failure = events.record_failure
    gap_counts = []

    def record_after_reclaim(session, actor, event_type, object_type, object_id=None, **kwargs):
        if object_id == datasource_id and kwargs.get("error_code") == "CollectionFailed":
            # 正常失败已提交 run.failed，guard 尚未写审计，Job 仍是 running。
            with session_scope() as checking:
                job = checking.get(Job, job_id)
                assert job.status == "running"
                run = checking.get(CollectionRun, job.result["run_id"])
                assert run.status == "failed" and run.finished_at is not None
                job.started_at = utcnow() - timedelta(minutes=5)
            assert job_id in jobs.abandoned_jobs(older_than_seconds=60)
            with session_scope() as checking:
                gap_counts.append(
                    len(list(checking.scalars(select(AuditEvent).where(AuditEvent.object_id == datasource_id))))
                )
        return record_failure(session, actor, event_type, object_type, object_id, **kwargs)

    monkeypatch.setattr(events, "record_failure", record_after_reclaim)
    jobs.run_job(job_id)
    assert gap_counts == [0]
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
