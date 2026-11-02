"""治理事件从真实业务入口产生；SQLite 验证事务，哈希链另由 PostgreSQL 迁移测试验证。"""

from contextlib import contextmanager, nullcontext
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request
from sqlalchemy import JSON, MetaData, create_engine, event, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session, sessionmaker

from avelis import cache, events, jobs
from avelis.api import auth, deps, imports
from avelis.collect import service as collect
from avelis.config import build_settings
from avelis.connectors.base import ObservedAsset, ObservedColumn, Snapshot
from avelis.connectors.ddl import AuditUnavailable
from avelis.db import Base, utcnow
from avelis.governance import changes, joins, metrics
from avelis.importer import batch
from avelis.importer.archive import ImportFile
from avelis.models import (
    Asset,
    AuditEvent,
    ChangeSet,
    CollectionRun,
    ColumnFingerprint,
    ColumnVersion,
    Datasource,
    Domain,
    DomainMember,
    EventOutbox,
    ImportBatch,
    Job,
    LineageEdge,
    Metric,
    MetricVersion,
    ReviewTask,
    User,
    UserRole,
    WebhookSubscription,
)
from avelis.security import hash_password


@pytest.fixture
def state(monkeypatch, tmp_path):
    settings = build_settings({"AVELIS_SECRET": "test-secret-events-x"})
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    monkeypatch.setattr(metrics, "get_settings", lambda: settings)
    monkeypatch.setattr(cache, "invalidate_tag", lambda _tag: 0)
    engine = create_engine(f"sqlite:///{(tmp_path / 'events.sqlite').as_posix()}")
    metadata = MetaData()
    for table in Base.metadata.tables.values():
        copied = table.to_metadata(metadata)
        for column in copied.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(engine)

    @contextmanager
    def scope():
        with Session(engine, expire_on_commit=False) as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    for module in (collect, batch, jobs, events):
        monkeypatch.setattr(module, "session_scope", scope)
    monkeypatch.setattr(batch, "_assist", lambda *_args, **_kwargs: None)
    with scope() as session:
        actor = User(
            id="actor", username="actor", display_name="维护者", password_hash=hash_password("Correct-password-2026")
        )
        reviewer = User(id="reviewer", username="reviewer", display_name="复核者", password_hash="unused")
        session.add_all(
            [
                actor,
                reviewer,
                UserRole(user_id=actor.id, role="steward"),
                UserRole(user_id=reviewer.id, role="reviewer"),
                Domain(id="domain", code="domain", name="测试域", owner_id=actor.id),
                DomainMember(domain_id="domain", user_id=actor.id, role="owner"),
                Datasource(id="ds", name="测试源", kind="starrocks", host="offline.invalid", port=9030, options={}),
            ]
        )
    try:
        with Session(engine, expire_on_commit=False) as session:
            yield SimpleNamespace(
                engine=engine,
                session=session,
                scope=scope,
                actor=session.get(User, "actor"),
                reviewer=session.get(User, "reviewer"),
                settings=settings,
            )
    finally:
        engine.dispose()


def emitted(state, event_type):
    state.session.flush()
    return [
        event
        for row in state.session.scalars(select(AuditEvent).order_by(AuditEvent.at, AuditEvent.id))
        if (event := events.event_from_audit(row)) is not None and event.type == event_type
    ]


def assert_emitted(state, event_type, object_id, *, outcome="success"):
    matching = [event for event in emitted(state, event_type) if event.object.id == object_id]
    assert len(matching) == 1, (event_type, matching)
    assert matching[0].outcome == outcome
    assert matching[0].id
    assert matching[0].at.utcoffset() == timedelta(0)
    return matching[0]


def subscribe_failure(state, event_type):
    subscription = WebhookSubscription(
        id="failure-hook",
        name="failure hook",
        url="https://example.invalid/hook",
        secret_enc="unused",
        event_types=[event_type],
        enabled=True,
    )
    state.session.add(subscription)
    state.session.commit()
    return subscription.id


def assert_failure_delivery(session, event_type, object_id, subscription_id="failure-hook"):
    session.flush()
    audits = list(
        session.scalars(select(AuditEvent).where(AuditEvent.action == event_type, AuditEvent.object_id == object_id))
    )
    assert len(audits) == 1
    outbox = list(session.scalars(select(EventOutbox).where(EventOutbox.subscription_id == subscription_id)))
    assert len(outbox) == 1
    assert outbox[0].event_id == audits[0].id
    assert outbox[0].payload["type"] == event_type
    assert outbox[0].payload["outcome"] == "failure"
    return events.event_from_audit(audits[0])


def trace_failed_transaction(state, monkeypatch, module, model, event_type, object_id):
    """在提交 failed 的会话内断言审计与发件箱，避免事后 guard 掩盖事务空档。"""
    observed = []

    @contextmanager
    def scope():
        with state.scope() as session:
            yield session
            session.flush()
            if session.scalar(select(model).where(model.status == "failed")) is not None:
                observed.append(assert_failure_delivery(session, event_type, object_id))

    monkeypatch.setattr(module, "session_scope", scope)
    return observed


def metric_version(state, *, version_no=1):
    session = state.session
    if session.get(Metric, "metric") is None:
        session.add(
            Metric(
                id="metric", code="METRIC", name="测试指标", domain_id="domain", owner_id="actor", created_by="actor"
            )
        )
    version = MetricVersion(
        metric_id="metric",
        version_no=version_no,
        status="draft",
        description="测试指标定义",
        expression="SUM(amount)",
        time_column="at",
        timezone="Asia/Shanghai",
        created_by="actor",
    )
    session.add(version)
    session.flush()
    return version


def publish_check(monkeypatch):
    monkeypatch.setattr(metrics, "publish_check", lambda *_: {"ok": True, "dependency": [], "blockers": []})


def test_metric_submit_and_publish_emit_real_transitions(state, monkeypatch):
    publish_check(monkeypatch)
    version = metric_version(state)
    metrics.submit(state.session, state.actor, version)
    submitted = assert_emitted(state, "metric_version.submitted", version.id)
    assert submitted.actor.id == state.actor.id
    assert version.status == "pending_review"
    metrics.approve(state.session, state.reviewer, version, "依赖已核实")
    published = assert_emitted(state, "metric_version.published", version.id)
    assert published.actor.id == state.reviewer.id
    assert published.id != submitted.id
    assert version.status == "published"


def test_metric_reject_emits_rejection(state, monkeypatch):
    publish_check(monkeypatch)
    version = metric_version(state)
    metrics.submit(state.session, state.actor, version)
    metrics.reject(state.session, state.reviewer, version, "需要补充口径依据")
    assert version.status == "rejected"
    assert_emitted(state, "metric_version.rejected", version.id)


def seed_asset(state, name, *, domain_id="domain"):
    state.session.add(
        Asset(
            id=name,
            datasource_id="ds",
            kind="table",
            catalog_name="default_catalog",
            db_name="src",
            name=name,
            domain_id=domain_id,
            current_version_id=f"{name}_v",
        )
    )
    state.session.add(
        ColumnVersion(asset_version_id=f"{name}_v", column_id=f"{name}_id", ordinal=1, name="id", data_type="INT")
    )
    state.session.add(ColumnFingerprint(asset_id=name, column_name="id", fingerprint=f"fp_{name}", status="ok"))
    state.session.flush()


def create_join(state):
    return joins.create(
        state.session,
        state.actor,
        code="records_join",
        name="记录关联",
        left_asset_id="left",
        left_columns=["id"],
        right_asset_id="right",
        right_columns=["id"],
    )


def test_join_approved_only_after_every_domain_approves(state):
    state.session.add(Domain(id="other", code="other", name="另一域", owner_id=state.reviewer.id))
    seed_asset(state, "left")
    seed_asset(state, "right", domain_id="other")
    path = create_join(state)
    joins.decide(state.session, state.actor, path, "domain", "approved", "本域批准")
    assert path.status == "pending"
    assert emitted(state, "join_path.approved") == []
    joins.decide(state.session, state.reviewer, path, "other", "approved", "另一域批准")
    assert path.status == "approved"
    assert_emitted(state, "join_path.approved", path.id)


def test_dependency_change_emits_metric_and_join_affected_then_invalidated(state):
    seed_asset(state, "left")
    seed_asset(state, "right")
    path = create_join(state)
    joins.decide(state.session, state.actor, path, "domain", "approved", "批准")
    version = metric_version(state)
    version.status = "published"
    version.dependency = [
        {"asset_id": "left", "column": "id", "qualified": "default_catalog.src.left", "fingerprint": "fp_left"}
    ]
    current = state.session.scalar(select(ColumnFingerprint).where(ColumnFingerprint.asset_id == "left"))
    current.fingerprint = "changed"
    change_set = ChangeSet(run_id="run", datasource_id="ds", status="open", summary={}, window_end=utcnow())
    state.session.add(change_set)
    state.session.flush()
    changes.analyze_change_set(
        state.session,
        change_set,
        [{"asset_id": "left", "column": "id", "before": {"fingerprint": "fp_left"}, "after": {"status": "ok"}}],
    )
    assert version.status == "affected" and path.status == "affected"
    assert_emitted(state, "metric_version.affected", version.id)
    assert_emitted(state, "join_path.affected", path.id)
    task = state.session.scalar(select(ReviewTask).where(ReviewTask.object_type == "metric_version"))
    changes.resolve_task(state.session, state.actor, task, "retired", "上游定义已失效，停用原口径")
    assert version.status == "retired" and task.status == "resolved"
    assert_emitted(state, "metric_version.invalidated", version.id)


class SnapshotConnector:
    ddl_audit_source = "offline_fixture"

    def __init__(self, *, cause="no_permission"):
        self.data_type = "INT"
        self.cause = cause

    def snapshot(self):
        return Snapshot(
            server_identity={"instance_id": "offline_fixture"},
            assets=[
                ObservedAsset(
                    catalog="default_catalog",
                    db="src",
                    name="records",
                    kind="table",
                    physical_id="table-1",
                    columns=[ObservedColumn("id", self.data_type, 1)],
                )
            ],
        )

    def ddl_events(self, *_args, **_kwargs):
        raise AuditUnavailable("来源不可读，password=must-not-escape", cause=self.cause)


@pytest.mark.parametrize("cause", ["no_permission", "not_enabled"])
def test_collection_completion_schema_change_and_attribution_from_real_snapshot(state, monkeypatch, cause):
    connector = SnapshotConnector(cause=cause)
    monkeypatch.setattr(collect, "collect_connector", lambda *_: connector)
    first_id = collect.run_collection("ds", trigger="manual", actor_id="actor")
    first_completed = emitted(state, "collect.completed")
    assert len(first_completed) == 1 and first_completed[0].detail["run_id"] == first_id
    assert emitted(state, "attribution.unavailable") == []
    connector.data_type = "BIGINT"
    second_id = collect.run_collection("ds", trigger="manual", actor_id="actor")
    state.session.expire_all()
    assert state.session.get(CollectionRun, second_id).status == "succeeded"
    assert {event.detail["run_id"] for event in emitted(state, "collect.completed")} == {first_id, second_id}
    schema_events = emitted(state, "collect.schema_changed")
    changed = next(event for event in schema_events if event.detail["run_id"] == second_id)
    assert changed.outcome == "success"
    assert changed.detail["changes"]["column_type_changed"] == 1
    unavailable = assert_emitted(state, "attribution.unavailable", "ds")
    assert unavailable.detail["cause"] == cause
    assert "must-not-escape" not in unavailable.model_dump_json()


def test_collection_failure_rolls_back_snapshot_and_emits_failure(state, monkeypatch):
    subscribe_failure(state, "collect.failed")
    observed = trace_failed_transaction(state, monkeypatch, collect, CollectionRun, "collect.failed", "ds")
    connector = SnapshotConnector()
    monkeypatch.setattr(collect, "collect_connector", lambda *_: connector)
    apply = collect._apply_snapshot
    original = RuntimeError("snapshot could not finish")

    def interrupted(*args):
        apply(*args)
        raise original

    monkeypatch.setattr(collect, "_apply_snapshot", interrupted)
    with pytest.raises(collect.CollectionFailed) as caught:
        collect.run_collection("ds", trigger="manual", actor_id="actor")
    assert caught.value.__cause__ is original
    assert caught.value.failure_event_recorded is True
    assert len(observed) == 1
    state.session.expire_all()
    assert list(state.session.scalars(select(Asset))) == []
    assert state.session.scalar(select(CollectionRun)).status == "failed"
    assert emitted(state, "collect.completed") == []
    assert emitted(state, "collect.schema_changed") == []
    failed = assert_emitted(state, "collect.failed", "ds", outcome="failure")
    assert failed.error.code
    assert_failure_delivery(state.session, "collect.failed", "ds")


def fail_status_transaction(state, monkeypatch, module, failure_at):
    fault = SimpleNamespace(armed=False, attempts=0)
    record_failure = events.record_failure

    def record(session, *args, **kwargs):
        row = record_failure(session, *args, **kwargs)
        if fault.armed and failure_at == "record" and fault.attempts == 0:
            fault.attempts += 1
            session.flush()
            raise RuntimeError("failed event transaction unavailable")
        return row

    @contextmanager
    def scope():
        with state.scope() as session:
            yield session
            if fault.armed and failure_at == "commit":
                fault.attempts += 1
                session.flush()
                raise RuntimeError("failed status transaction unavailable")

    monkeypatch.setattr(events, "record_failure", record)
    monkeypatch.setattr(module, "session_scope", scope)
    return fault


@pytest.mark.parametrize("failure_at", ["record", "commit"])
def test_collection_status_failure_does_not_mask_business_failure_or_block_event(
    state, monkeypatch, caplog, failure_at
):
    subscribe_failure(state, "collect.failed")
    monkeypatch.setattr(collect, "collect_connector", lambda *_: SnapshotConnector())
    fault = fail_status_transaction(state, monkeypatch, collect, failure_at)
    apply = collect._apply_snapshot
    original = ValueError("snapshot validation failed")

    def interrupted(*args):
        apply(*args)
        fault.armed = True
        raise original

    monkeypatch.setattr(collect, "_apply_snapshot", interrupted)
    with pytest.raises(collect.CollectionFailed) as caught:
        collect.run_collection("ds", trigger="manual", actor_id="actor")
    assert caught.value.__cause__ is original
    assert str(caught.value) == str(original)
    assert not getattr(caught.value, "failure_event_recorded", False)
    assert fault.attempts == 1
    state.session.expire_all()
    assert list(state.session.scalars(select(Asset))) == []
    assert state.session.scalar(select(CollectionRun)).status == "running"
    assert_emitted(state, "collect.failed", "ds", outcome="failure")
    assert_failure_delivery(state.session, "collect.failed", "ds")
    assert "采集失败状态未能写入" in caplog.text


def create_import(state):
    imported, created = batch.create_batch(
        state.session,
        state.actor,
        datasource_id="ds",
        files=[ImportFile("pipeline.sql", "CREATE TABLE dw.summary AS SELECT id FROM src.events;", "sql")],
    )
    assert created
    state.session.commit()
    return imported


def test_lineage_import_completion_emits_from_parser_and_persistence(state):
    seed_asset(state, "events")
    imported = create_import(state)
    result = batch.run_import(imported.id)
    state.session.expire_all()
    assert result["status"] == "done"
    assert list(state.session.scalars(select(LineageEdge)))
    completed = assert_emitted(state, "lineage_import.completed", imported.id)
    assert completed.detail["status"] == "done"


def test_lineage_import_failure_rolls_back_edges_and_preserves_original_error(state, monkeypatch):
    seed_asset(state, "events")
    imported = create_import(state)
    subscribe_failure(state, "lineage_import.failed")
    observed = trace_failed_transaction(state, monkeypatch, batch, ImportBatch, "lineage_import.failed", imported.id)
    persist = batch._persist_edge
    original = RuntimeError("edge transaction interrupted")

    def interrupted(*args, **kwargs):
        persist(*args, **kwargs)
        raise original

    monkeypatch.setattr(batch, "_persist_edge", interrupted)
    with pytest.raises(RuntimeError) as caught:
        batch.run_import(imported.id)
    assert caught.value is original
    assert caught.value.failure_event_recorded is True
    assert len(observed) == 1
    state.session.expire_all()
    assert state.session.get(ImportBatch, imported.id).status == "failed"
    assert list(state.session.scalars(select(LineageEdge))) == []
    assert emitted(state, "lineage_import.completed") == []
    assert_emitted(state, "lineage_import.failed", imported.id, outcome="failure")
    assert_failure_delivery(state.session, "lineage_import.failed", imported.id)


@pytest.mark.parametrize("failure_at", ["record", "commit"])
def test_import_status_failure_does_not_mask_business_failure_or_block_event(state, monkeypatch, caplog, failure_at):
    seed_asset(state, "events")
    imported = create_import(state)
    subscribe_failure(state, "lineage_import.failed")
    fault = fail_status_transaction(state, monkeypatch, batch, failure_at)
    persist = batch._persist_edge
    original = ValueError("edge persistence interrupted")

    def interrupted(*args, **kwargs):
        persist(*args, **kwargs)
        fault.armed = True
        raise original

    monkeypatch.setattr(batch, "_persist_edge", interrupted)
    with pytest.raises(ValueError) as caught:
        batch.run_import(imported.id)
    assert caught.value is original
    assert not getattr(caught.value, "failure_event_recorded", False)
    assert fault.attempts == 1
    state.session.expire_all()
    assert state.session.get(ImportBatch, imported.id).status == "running"
    assert list(state.session.scalars(select(LineageEdge))) == []
    assert_emitted(state, "lineage_import.failed", imported.id, outcome="failure")
    assert_failure_delivery(state.session, "lineage_import.failed", imported.id)
    assert "导入失败状态未能写入" in caplog.text


@pytest.mark.parametrize("status", ["queued", "running"])
@pytest.mark.parametrize("read", ["get", "list", "view"])
def test_import_reads_commit_reconciled_failure_and_only_emit_once(state, monkeypatch, status, read):
    imported = create_import(state)
    imported.status = status
    job = state.session.get(Job, imported.job_id)
    job.status, job.error = "failed", "worker failed with password=must-not-escape"
    subscribe_failure(state, "lineage_import.failed")
    monkeypatch.setattr(deps, "session_factory", lambda: sessionmaker(state.engine, expire_on_commit=False))

    def read_import(session):
        actor = session.get(User, "actor")
        if read == "get":
            return imports.get_import(imported.id, user=actor, session=session)
        if read == "list":
            return next(item for item in imports.list_imports(user=actor, session=session) if item["id"] == imported.id)
        return batch.batch_view(session, session.get(ImportBatch, imported.id))

    request = deps.get_session()
    session = next(request)
    with job_write_trace(state.engine) as trace:
        assert read_import(session)["status"] == "failed"
        # GET 依赖尚未退出：状态、审计和发件箱已经同处请求事务。
        failed = assert_failure_delivery(session, "lineage_import.failed", imported.id)
        assert failed.error.code == "WORKER_FAILED"
        assert failed.actor.id == imported.created_by
        assert failed.detail["job_id"] == imported.job_id
        assert "must-not-escape" not in failed.model_dump_json()
        assert read_import(session)["status"] == "failed"
        assert_failure_delivery(session, "lineage_import.failed", imported.id)
        assert trace[0] == "job_lock" and "import_batch" in trace
        with pytest.raises(StopIteration):
            next(request)  # 执行真实 get_session 的请求结束 commit。
    state.session.expire_all()
    assert state.session.get(ImportBatch, imported.id).status == "failed"
    assert_failure_delivery(state.session, "lineage_import.failed", imported.id)


@pytest.mark.parametrize("status", ["queued", "running"])
def test_reconcile_between_abandonment_transactions_keeps_exactly_one_failure(state, monkeypatch, status):
    imported = create_import(state)
    imported.status = status
    job = state.session.get(Job, imported.job_id)
    job.status, job.attempts = "running", 1
    job.started_at = utcnow() - timedelta(minutes=5)
    subscribe_failure(state, "lineage_import.failed")
    finish = jobs._record_abandoned_governance
    reconciled = []

    def reconcile_before_finish(job_id, kind, payload, result, reason):
        with state.scope() as session:
            saved = session.get(Job, job_id)
            assert saved.status == "failed" and saved.result["abandoned_governance"] == "pending"
            live = session.get(ImportBatch, imported.id)
            assert live.status == status
            batch.reconcile_batch(session, live)
            failed = assert_failure_delivery(session, "lineage_import.failed", imported.id)
            assert failed.error.code == "WORKER_FAILED"
            reconciled.append(failed.id)
        finish(job_id, kind, payload, result, reason)

    monkeypatch.setattr(jobs, "_record_abandoned_governance", reconcile_before_finish)
    assert jobs.abandoned_jobs(older_than_seconds=60) == [job.id]
    assert len(reconciled) == 1
    state.session.expire_all()
    assert state.session.get(ImportBatch, imported.id).status == "failed"
    assert state.session.get(Job, job.id).result["abandoned_governance"] == "done"
    failed = assert_failure_delivery(state.session, "lineage_import.failed", imported.id)
    assert failed.id == reconciled[0]
    assert jobs.abandoned_jobs(older_than_seconds=60) == []
    with pytest.raises(batch.ImportAbandoned):
        batch.run_import(imported.id, job_id=job.id, attempt=1)
    assert_failure_delivery(state.session, "lineage_import.failed", imported.id)


def test_login_failure_threshold_is_emitted_once_when_limit_is_reached(state, monkeypatch):
    class MemoryRedis:
        def __init__(self):
            self.values = {}

        def get(self, name):
            return self.values.get(name)

        def incr(self, name):
            self.values[name] = int(self.values.get(name, 0)) + 1
            return self.values[name]

        def expire(self, _name, _seconds):
            return True

        def ttl(self, _name):
            return 60

    store = MemoryRedis()
    monkeypatch.setattr(cache, "client", lambda: store)
    request = Request({"type": "http", "client": ("192.0.2.11", 12345), "headers": []})
    form = SimpleNamespace(username="actor", password="incorrect-password")
    for attempt in range(state.settings.login_max_failures):
        with pytest.raises(HTTPException) as caught:
            auth.login(request, form, state.session)
        assert caught.value.status_code == 401
        if attempt < state.settings.login_max_failures - 1:
            assert emitted(state, "auth.login_failure_threshold") == []
    event = assert_emitted(state, "auth.login_failure_threshold", "actor", outcome="failure")
    assert "incorrect-password" not in event.model_dump_json()
    for _ in range(2):
        with pytest.raises(HTTPException) as caught:
            auth.login(request, form, state.session)
        assert caught.value.status_code == 429
    assert len(emitted(state, "auth.login_failure_threshold")) == 1


@pytest.mark.parametrize(
    ("kind", "status"),
    [("collect", "running"), ("collect", "failed"), ("import", "queued"), ("import", "running"), ("import", "failed")],
)
def test_worker_abandonment_emits_failure_only_when_it_fails_business_object(state, monkeypatch, kind, status):
    started_at = utcnow() - timedelta(minutes=5)
    if kind == "import":
        imported = create_import(state)
        job = state.session.get(Job, imported.job_id)
        imported.status = status
        object_id, event_type = imported.id, "lineage_import.failed"
    else:
        run = CollectionRun(
            datasource_id="ds", trigger="manual", status=status, triggered_by="actor", started_at=started_at
        )
        state.session.add(run)
        state.session.flush()
        job = Job(kind="collect", payload={"datasource_id": "ds"}, created_by="actor", result={"run_id": run.id})
        state.session.add(job)
        object_id, event_type = "ds", "collect.failed"
    job.status, job.started_at = "running", started_at
    subscribe_failure(state, event_type)
    if status == "failed":
        # failed 对象来自此前已原子写入失败事件的路径；回收只补 Job 收尾标记。
        events.record_failure(
            state.session,
            "actor",
            event_type,
            "import_batch" if kind == "import" else "datasource",
            object_id,
            error_code="PREVIOUS_FAILURE",
            job_id=job.id,
        )
        state.session.commit()
    else:
        observed = trace_failed_transaction(
            state, monkeypatch, jobs, ImportBatch if kind == "import" else CollectionRun, event_type, object_id
        )
    assert jobs.abandoned_jobs(older_than_seconds=60) == [job.id]
    state.session.expire_all()
    saved = state.session.get(Job, job.id)
    assert saved.status == "failed" and saved.finished_at is not None and saved.error
    assert saved.result["abandoned_governance"] == "done"
    if kind == "import":
        assert state.session.get(ImportBatch, object_id).status == "failed"
    else:
        assert state.session.get(CollectionRun, run.id).status == "failed"
    event = assert_emitted(state, event_type, object_id, outcome="failure")
    assert event.error.code == ("PREVIOUS_FAILURE" if status == "failed" else "WORKER_ABANDONED")
    if status != "failed":
        assert len(observed) == 1
    assert jobs.abandoned_jobs(older_than_seconds=60) == []
    assert_failure_delivery(state.session, event_type, object_id)


@pytest.mark.parametrize(
    ("kind", "association"),
    [
        ("collect", "legacy"),
        ("collect", "missing"),
        ("collect", "mismatched"),
        ("import", "legacy"),
        ("import", "missing"),
    ],
)
def test_worker_abandonment_without_exact_business_object_still_emits_failure(state, kind, association):
    payload = {"datasource_id": "ds"} if kind == "collect" else {}
    result = {}
    if association != "legacy":
        if kind == "collect":
            result["run_id"] = "missing-run"
            if association == "mismatched":
                state.session.add(
                    Datasource(id="other-ds", name="另一源", kind="postgres", host="offline.invalid", port=1)
                )
                state.session.add(
                    CollectionRun(id="missing-run", datasource_id="other-ds", trigger="manual", status="failed")
                )
        else:
            payload["batch_id"] = "missing-batch"
    job = Job(kind=kind, payload=payload, result=result, status="running", started_at=utcnow() - timedelta(minutes=5))
    state.session.add(job)
    state.session.commit()

    assert jobs.abandoned_jobs(older_than_seconds=60) == [job.id]
    assert jobs.abandoned_jobs(older_than_seconds=60) == []
    state.session.expire_all()
    saved = state.session.get(Job, job.id)
    assert saved.status == "failed" and saved.result["abandoned_governance"] == "done"
    event_type = "collect.failed" if kind == "collect" else "lineage_import.failed"
    failed = assert_emitted(
        state, event_type, payload.get("datasource_id" if kind == "collect" else "batch_id"), outcome="failure"
    )
    assert failed.error.code == "WORKER_ABANDONED" and failed.detail["job_id"] == job.id
    assert len(list(state.session.scalars(select(AuditEvent).where(AuditEvent.action == event_type)))) == 1


@pytest.mark.parametrize("kind", ["collect", "import"])
@pytest.mark.parametrize("failure_at", ["record", "commit"])
def test_worker_retries_abandoned_governance_atomically_without_duplicate_events(state, monkeypatch, kind, failure_at):
    started_at = utcnow() - timedelta(minutes=5)
    if kind == "import":
        imported = create_import(state)
        job = state.session.get(Job, imported.job_id)
        imported.status = "running"
        model, object_id, event_type = ImportBatch, imported.id, "lineage_import.failed"
    else:
        run = CollectionRun(datasource_id="ds", trigger="manual", status="running", started_at=started_at)
        state.session.add(run)
        state.session.flush()
        job = Job(kind="collect", payload={"datasource_id": "ds"}, result={"run_id": run.id})
        state.session.add(job)
        model, object_id, event_type = CollectionRun, run.id, "collect.failed"
    job.status, job.started_at = "running", started_at
    state.session.add(
        WebhookSubscription(
            id="abandoned-hook",
            name="hook",
            url="https://example.invalid/hook",
            secret_enc="unused",
            event_types=[event_type],
            enabled=True,
        )
    )
    state.session.commit()
    original = events.record_failure

    def fail_after_record(session, *args, **kwargs):
        original(session, *args, **kwargs)
        session.flush()
        raise RuntimeError("temporary database failure")

    commits = []

    @contextmanager
    def fail_second_commit():
        with state.scope() as session:
            yield session
            commits.append(session)
            if len(commits) == 2:
                session.flush()
                raise RuntimeError("temporary commit failure")

    with monkeypatch.context() as patch:
        if failure_at == "record":
            patch.setattr(events, "record_failure", fail_after_record)
        else:
            patch.setattr(jobs, "session_scope", fail_second_commit)
        assert jobs.abandoned_jobs(older_than_seconds=60) == [job.id]
    state.session.expire_all()
    saved = state.session.get(Job, job.id)
    assert saved.status == "failed" and saved.result["abandoned_governance"] == "pending"
    reason = saved.error
    assert state.session.get(model, object_id).status == "running"
    assert emitted(state, event_type) == []
    assert list(state.session.scalars(select(EventOutbox))) == []
    state.session.rollback()

    # 配置/扫描阈值改变时也必须使用原超时原因重试；返回值仍只报告本次新回收的任务。
    assert jobs.abandoned_jobs(older_than_seconds=120) == []
    state.session.expire_all()
    saved = state.session.get(Job, job.id)
    assert saved.status == "failed" and saved.result["abandoned_governance"] == "done"
    assert state.session.get(model, object_id).status == "failed"
    assert len(emitted(state, event_type)) == 1
    assert len(list(state.session.scalars(select(EventOutbox)))) == 1
    state.session.rollback()

    # 已拿到待收尾快照的另一轮/worker 重复调用，也不应重复写审计或事件。
    jobs._record_abandoned_governance(job.id, kind, job.payload, job.result, reason)
    assert jobs.abandoned_jobs(older_than_seconds=120) == []
    assert len(emitted(state, event_type)) == 1
    assert len(list(state.session.scalars(select(EventOutbox)))) == 1


@pytest.mark.parametrize("kind", ["collect", "import"])
def test_worker_reconciles_completed_business_instead_of_emitting_false_failure(state, monkeypatch, kind):
    started_at = utcnow() - timedelta(minutes=5)
    if kind == "import":
        seed_asset(state, "events")
        imported = create_import(state)
        assert batch.run_import(imported.id)["status"] == "done"
        state.session.expire_all()
        job = state.session.get(Job, imported.job_id)
        event_type = "lineage_import.failed"
    else:
        connector = SnapshotConnector()
        monkeypatch.setattr(collect, "collect_connector", lambda *_: connector)
        run_id = collect.run_collection("ds", trigger="manual", actor_id="actor")
        job = Job(kind="collect", payload={"datasource_id": "ds"}, created_by="actor", result={"run_id": run_id})
        state.session.add(job)
        event_type = "collect.failed"
    job.status, job.started_at = "running", started_at
    state.session.commit()
    assert jobs.abandoned_jobs(older_than_seconds=60) == [job.id]
    state.session.expire_all()
    assert state.session.get(Job, job.id).status == "succeeded"
    assert emitted(state, event_type) == []


@pytest.mark.parametrize("snapshot_fails", [False, True])
def test_abandoned_collection_cannot_commit_late_snapshot_or_duplicate_failure(state, monkeypatch, snapshot_fails):
    clock = [utcnow()]
    monkeypatch.setattr(jobs, "utcnow", lambda: clock[0])
    monkeypatch.setattr(jobs, "advisory_lock", lambda _key: nullcontext())
    job_id = jobs._enqueue(state.session, "collect", {"datasource_id": "ds", "trigger": "manual"}, "actor")
    state.session.commit()

    def snapshot_after_abandonment():
        with state.scope() as session:
            assert session.get(Job, job_id).result["run_id"] == session.scalar(select(CollectionRun.id))
        clock[0] += timedelta(seconds=60)
        assert jobs.abandoned_jobs(older_than_seconds=30) == [job_id]
        if snapshot_fails:
            raise RuntimeError("snapshot failed after timeout")
        return SnapshotConnector().snapshot()

    monkeypatch.setattr(collect, "collect_connector", lambda *_: SimpleNamespace(snapshot=snapshot_after_abandonment))
    jobs.run_job(job_id)
    state.session.expire_all()
    assert state.session.get(Job, job_id).status == "failed"
    assert state.session.scalar(select(CollectionRun)).status == "failed"
    assert list(state.session.scalars(select(Asset))) == []
    assert emitted(state, "collect.completed") == []
    assert emitted(state, "collect.schema_changed") == []
    failed = assert_emitted(state, "collect.failed", "ds", outcome="failure")
    assert failed.error.code == "WORKER_ABANDONED"


@contextmanager
def job_write_trace(engine):
    """SQLite 不执行 FOR UPDATE；在 ORM 层记录锁语句，在引擎层记录真实 UPDATE。"""
    trace = []

    def lock_statement(state):
        statement = state.statement
        if getattr(statement, "_for_update_arg", None) is not None and any(
            column.get("entity") is Job for column in statement.column_descriptions
        ):
            trace.append("job_lock")

    def write_statement(_conn, _cursor, statement, _params, _context, _many):
        for table in ("collection_run", "import_batch"):
            if statement.startswith(f"UPDATE {table} SET"):
                trace.append(table)

    event.listen(Session, "do_orm_execute", lock_statement)
    event.listen(engine, "before_cursor_execute", write_statement)
    try:
        yield trace
    finally:
        event.remove(Session, "do_orm_execute", lock_statement)
        event.remove(engine, "before_cursor_execute", write_statement)


@pytest.mark.parametrize("apply_fails", [False, True])
def test_collection_locks_job_before_snapshot_or_failure_writes(state, monkeypatch, apply_fails):
    job = Job(kind="collect", payload={"datasource_id": "ds"}, status="running", attempts=1)
    state.session.add(job)
    state.session.commit()
    connector = SnapshotConnector()
    monkeypatch.setattr(collect, "collect_connector", lambda *_: connector)
    apply = collect._apply_snapshot

    with job_write_trace(state.engine) as trace:

        def snapshot():
            trace.clear()  # 只观察快照返回后的事务，不让创建 run 时的 Job 锁掩盖问题。
            return SnapshotConnector().snapshot()

        monkeypatch.setattr(connector, "snapshot", snapshot)

        def apply_snapshot(*args):
            apply(*args)
            if apply_fails:
                raise RuntimeError("snapshot persistence failed")

        monkeypatch.setattr(collect, "_apply_snapshot", apply_snapshot)
        expected = pytest.raises(collect.CollectionFailed) if apply_fails else nullcontext()
        with expected:
            collect.run_collection("ds", trigger="manual", job_id=job.id, attempt=1)
        assert "collection_run" in trace
        assert trace[0] == "job_lock", trace
        if apply_fails:
            assert trace.count("job_lock") == 2  # 失败收尾也先锁 Job。


@pytest.mark.parametrize("phase", ["start", "finish", "failure"])
def test_abandoned_import_cannot_reconcile_or_write_duplicate_failure(state, monkeypatch, phase):
    imported = create_import(state)
    with state.scope() as session:
        job = session.get(Job, imported.job_id)
        job.status, job.attempts = "running", 1
        job.started_at = utcnow() - timedelta(minutes=5)

    def abandon(*_args, **_kwargs):
        assert jobs.abandoned_jobs(older_than_seconds=60) == [imported.job_id]
        if phase == "failure":
            raise RuntimeError("model failed after timeout")

    if phase == "start":
        abandon()
    else:
        monkeypatch.setattr(batch, "_assist", abandon)
    with job_write_trace(state.engine) as trace:
        with pytest.raises(batch.ImportAbandoned):
            batch.run_import(imported.id, job_id=imported.job_id, attempt=1)
    state.session.expire_all()
    assert state.session.get(ImportBatch, imported.id).status == "failed"
    failed = assert_emitted(state, "lineage_import.failed", imported.id, outcome="failure")
    assert failed.error.code == "WORKER_ABANDONED"
    assert emitted(state, "lineage_import.completed") == []
    if phase == "start":
        assert trace == ["job_lock"]  # 失去执行权后不写批次。


@pytest.mark.parametrize("legacy", [False, True], ids=["bound_run", "legacy_without_run_id"])
def test_old_abandoned_job_never_fails_new_run_for_same_datasource_and_actor(state, legacy):
    old_start = utcnow() - timedelta(minutes=5)
    reason = "worker 超时，正在收尾"
    old_run = CollectionRun(
        id="old_run", datasource_id="ds", trigger="manual", status="running", triggered_by="actor", started_at=old_start
    )
    old_job = Job(
        id="old_job",
        kind="collect",
        payload={"datasource_id": "ds"},
        created_by="actor",
        status="failed",
        started_at=old_start,
        error=reason,
        result={} if legacy else {"run_id": old_run.id},
    )
    state.session.add_all([old_run, old_job])
    state.session.commit()
    # 旧 Job 的回收事务已经提交；独立收尾之前，另一个 worker 已开始同源、同操作者的新采集。
    new_run = CollectionRun(
        id="new_run", datasource_id="ds", trigger="manual", status="running", triggered_by="actor", started_at=utcnow()
    )
    new_job = Job(
        id="new_job",
        kind="collect",
        payload={"datasource_id": "ds"},
        created_by="actor",
        status="running",
        started_at=utcnow(),
        result={"run_id": new_run.id},
    )
    state.session.add_all([new_run, new_job])
    state.session.commit()
    jobs._record_abandoned_governance(old_job.id, "collect", old_job.payload, old_job.result, reason)
    state.session.expire_all()
    assert state.session.get(Job, new_job.id).status == "running"
    assert state.session.get(CollectionRun, new_run.id).status == "running"
    assert state.session.get(CollectionRun, old_run.id).status == ("running" if legacy else "failed")
    failed = assert_emitted(state, "collect.failed", "ds", outcome="failure")
    assert failed.error.code == "WORKER_ABANDONED"
    assert failed.detail["job_id"] == old_job.id
