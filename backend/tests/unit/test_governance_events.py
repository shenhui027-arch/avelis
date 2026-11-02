"""治理契约、审计同事务发件箱与业务失败后的独立留底。"""

import json
from contextlib import contextmanager

import pytest
from pydantic import ValidationError
from sqlalchemy import JSON, MetaData, create_engine, select
from sqlalchemy import event as sa_event
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from avelis import audit, events
from avelis.db import Base
from avelis.events import EventType, GovernanceEvent
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


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'events.db'}")
    metadata = MetaData()
    for source in Base.metadata.sorted_tables:
        table = source.to_metadata(metadata)
        for column in table.columns:
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

    monkeypatch.setattr(events, "session_scope", scope)
    with scope() as session:
        session.add(User(id="actor", username="reviewer", display_name="复核人", password_hash="unused"))
    yield scope
    engine.dispose()


def subscription(session, identifier="sub", *, types=None, outcomes=None, enabled=True):
    row = WebhookSubscription(
        id=identifier,
        name=identifier,
        url="https://example.invalid/hook",
        secret_enc="unused",
        event_types=types or [EventType.METRIC_VERSION_PUBLISHED],
        outcomes=outcomes,
        enabled=enabled,
    )
    session.add(row)
    session.flush()
    return row


def test_contract_and_projection_exclude_secrets_sql_rows_and_free_text(database):
    with database() as session:
        subscription(session)
        row = audit.record(
            session,
            session.get(User, "actor"),
            "metric_version.approve",
            "metric_version",
            "version",
            note="unrecognizable-private-value",
            password="private-pass",
            token="private-token",
            sql="SELECT private_column FROM private_table",
            rows=[{"value": "private-row"}],
            safe={"nested": {"secret": "private-nested", "count": 2}},
            run_id="run",
            reason="password=private-reason",
            api_key="private-key",
        )
        assert "_governance" in row.detail
        event_id = row.id
    with database() as session:
        row = session.get(AuditEvent, event_id)
        assert row.detail["safe"] == {"nested": {"count": 2}}
        item = session.scalar(select(EventOutbox))
        event = GovernanceEvent.model_validate(item.payload)
        assert item.event_id == event.id == event_id
        assert event.type == EventType.METRIC_VERSION_PUBLISHED and event.outcome == "success"
        assert event.actor.model_dump() == {"id": "actor", "name": "reviewer"}
        assert event.object.model_dump(exclude_none=True) == {"type": "metric_version", "id": "version"}
        assert event.at.utcoffset().total_seconds() == 0
        assert event.detail == {"run_id": "run"} and event.error is None
        assert set(item.payload) == {"id", "type", "outcome", "at", "actor", "object", "detail"}
        serialized = json.dumps(item.payload)
        assert not any(value in serialized for value in ("private-", "SELECT", "unrecognizable"))
        assert row.detail["note"] == "unrecognizable-private-value"
        assert not any(value in json.dumps(row.detail) for value in ("private-pass", "private-token", "private-key"))
        with pytest.raises(ValidationError):
            GovernanceEvent.model_validate({**item.payload, "extra": "not in contract"})


def test_outbox_matches_type_outcome_and_enabled_and_rolls_back_atomically(database):
    with database() as session:
        subscription(session, "yes")
        subscription(session, "failure-only", outcomes=["failure"])
        subscription(session, "other", types=[EventType.COLLECT_COMPLETED])
        subscription(session, "disabled", enabled=False)
        audit.record(session, None, "metric_version.approve", "metric_version", "version")
        audit.record(session, None, "unmapped.action", "metric_version", "version")
    with database() as session:
        assert [row.subscription_id for row in session.scalars(select(EventOutbox))] == ["yes"]
        assert len(list(session.scalars(select(AuditEvent)))) == 2
    with pytest.raises(RuntimeError), database() as session:
        audit.record(session, None, "metric_version.approve", "metric_version", "rolled-back")
        session.flush()
        assert len(list(session.scalars(select(EventOutbox)))) == 2
        raise RuntimeError("rollback")
    with database() as session:
        assert len(list(session.scalars(select(EventOutbox)))) == 1
        assert len(list(session.scalars(select(AuditEvent)))) == 2


def test_failure_guard_rolls_back_business_and_commits_failure_with_original_identity(database):
    with database() as session:
        subscription(session, outcomes=["failure"])
    original = RuntimeError("password=secret; SELECT private FROM source")
    with database() as business:
        actor = business.get(User, "actor")
        with pytest.raises(RuntimeError) as caught:
            with events.failure_guard(
                EventType.METRIC_VERSION_PUBLISHED, actor, "metric_version", "v", session=business
            ):
                actor.display_name = "must rollback"
                audit.record(business, actor, "metric_version.approve", "metric_version", "v")
                business.flush()
                raise original
        assert caught.value is original
    with database() as session:
        assert session.get(User, "actor").display_name == "复核人"
        rows = list(session.scalars(select(AuditEvent)))
        assert len(rows) == 1
        failure = rows[0]
        assert failure.actor_id == "actor" and failure.actor_name == "reviewer"
        assert failure.detail["_governance"] == {
            "type": "metric_version.published",
            "outcome": "failure",
            "error": {"code": "RuntimeError", "reason": "操作失败"},
        }
        item = session.scalar(select(EventOutbox))
        assert item.event_id == failure.id and item.payload["outcome"] == "failure"
        assert "secret" not in json.dumps(item.payload) and "SELECT" not in json.dumps(item.payload)
        # SQLite 不执行 PostgreSQL 提交触发器；真实封链断言在 tests/db 中完成。
        assert AuditEvent.__table__.c.chain_seq is not None


def test_failure_guard_handles_commit_error(database, monkeypatch):
    with database() as session:
        subscription(session, types=[EventType.COLLECT_FAILED], outcomes=["failure"])
    original = RuntimeError("commit failed")
    with database() as business:

        def reject_commit():
            raise original

        with pytest.raises(RuntimeError) as caught, monkeypatch.context() as patch:
            patch.setattr(business, "commit", reject_commit)
            with events.failure_guard(EventType.COLLECT_FAILED, "actor", "datasource", "ds", session=business):
                business.add(User(id="rollback", username="rollback", display_name="rollback", password_hash="unused"))
                events.record_failure(business, "actor", EventType.COLLECT_FAILED, "datasource", "ds")
                business.flush()
                assert len(list(business.scalars(select(EventOutbox)))) == 1
                business.commit()
        assert caught.value is original
    with database() as session:
        assert session.get(User, "rollback") is None
        rows = list(session.scalars(select(AuditEvent)))
        assert len(rows) == 1
        row = rows[0]
        assert row.actor_id == "actor"
        assert row.detail["_governance"]["outcome"] == "failure"
        outbox = list(session.scalars(select(EventOutbox)))
        assert len(outbox) == 1 and outbox[0].event_id == row.id


def test_failure_guard_skips_failure_already_committed_with_state(database, monkeypatch):
    from avelis.collect.service import CollectionFailed

    with database() as session:
        subscription(session, types=[EventType.COLLECT_FAILED], outcomes=["failure"])
        events.record_failure(session, "actor", EventType.COLLECT_FAILED, "datasource", "ds", run_id="run")

    def unexpected():
        pytest.fail("recorded failure opened another failure transaction")

    monkeypatch.setattr(events, "session_scope", unexpected)
    original = CollectionFailed("failure already committed")
    original.failure_event_recorded = True
    with pytest.raises(CollectionFailed) as caught:
        with events.failure_guard(EventType.COLLECT_FAILED, "actor", "datasource", "ds"):
            raise original
    assert caught.value is original
    with database() as session:
        rows = list(session.scalars(select(AuditEvent)))
        outbox = list(session.scalars(select(EventOutbox)))
        assert len(rows) == len(outbox) == 1
        assert outbox[0].event_id == rows[0].id


@pytest.mark.parametrize(
    ("event_type", "object_id", "job_id", "attempt", "expected"),
    [
        (EventType.LINEAGE_IMPORT_FAILED, "batch", "job", 1, True),
        (EventType.COLLECT_FAILED, "batch", "job", 1, False),
        (EventType.LINEAGE_IMPORT_FAILED, "other-batch", "job", 1, False),
        (EventType.LINEAGE_IMPORT_FAILED, "batch", "other-job", 1, False),
        (EventType.LINEAGE_IMPORT_FAILED, "batch", "job", 2, False),
        (EventType.LINEAGE_IMPORT_FAILED, "batch", None, 1, False),
        (EventType.LINEAGE_IMPORT_FAILED, "batch", "job", None, False),
    ],
)
def test_failure_deduplication_requires_the_complete_execution_key(
    database, event_type, object_id, job_id, attempt, expected
):
    with database() as session:
        events.record_failure(
            session,
            "actor",
            EventType.LINEAGE_IMPORT_FAILED,
            "import_batch",
            "batch",
            job_id="job",
            attempt=1,
        )
    # A new session must find the committed audit, rather than process-local state.
    with database() as session:
        assert events.failure_already_recorded(session, event_type, object_id, job_id, attempt) is expected


def test_failure_guard_without_job_id_does_not_deduplicate_synchronous_calls(database):
    with database() as session:
        subscription(session, types=[EventType.COLLECT_FAILED], outcomes=["failure"])
    for _ in range(2):
        original = RuntimeError("synchronous collection failed")
        with pytest.raises(RuntimeError) as caught:
            with events.failure_guard(EventType.COLLECT_FAILED, "actor", "datasource", "ds"):
                raise original
        assert caught.value is original
    with database() as session:
        assert len(list(session.scalars(select(AuditEvent)))) == 2
        assert len(list(session.scalars(select(EventOutbox)))) == 2


def seed_guard_execution(database, kind, *, status="running"):
    event_type = EventType.LINEAGE_IMPORT_FAILED if kind == "import" else EventType.COLLECT_FAILED
    with database() as session:
        session.add(Datasource(id="ds", name="source", kind="postgres", host="offline.invalid", port=5432))
        job = Job(
            id="job",
            kind=kind,
            status="running",
            attempts=1,
            created_by="actor",
            payload={"batch_id": "batch"} if kind == "import" else {"datasource_id": "ds"},
            result={} if kind == "import" else {"run_id": "run"},
        )
        session.add(job)
        if kind == "import":
            session.add(ImportBatch(id="batch", datasource_id="ds", created_by="actor", job_id=job.id, status=status))
        else:
            session.add(
                CollectionRun(id="run", datasource_id="ds", triggered_by="actor", trigger="manual", status=status)
            )
        subscription(session, types=[event_type], outcomes=["failure"])
    return (
        event_type,
        "import_batch" if kind == "import" else "datasource",
        "batch" if kind == "import" else "ds",
        ImportBatch if kind == "import" else CollectionRun,
        "batch" if kind == "import" else "run",
    )


@pytest.mark.parametrize("kind", ["import", "collect"])
@pytest.mark.parametrize("status", ["queued", "running"])
def test_failure_guard_commits_object_failure_and_one_event_per_attempt(database, kind, status):
    event_type, object_type, object_id, model, model_id = seed_guard_execution(database, kind, status=status)
    for attempt in (1, 2):
        if attempt == 2:
            with database() as session:
                session.get(Job, "job").attempts = attempt
                session.get(model, model_id).status = "running"
        for _ in range(2):
            original = RuntimeError("business failure")
            with pytest.raises(RuntimeError) as caught:
                # Omitting attempt must resolve the current persisted Job.attempts.
                with events.failure_guard(event_type, "actor", object_type, object_id, job_id="job"):
                    raise original
            assert caught.value is original
        with database() as session:
            assert session.get(model, model_id).status == "failed"
            audits = list(session.scalars(select(AuditEvent).order_by(AuditEvent.at, AuditEvent.id)))
            assert sorted(row.detail["attempt"] for row in audits) == list(range(1, attempt + 1))
            assert all(row.detail["job_id"] == "job" for row in audits)
            assert len(list(session.scalars(select(EventOutbox)))) == attempt


@pytest.mark.parametrize("kind", ["import", "collect"])
def test_failure_guard_object_write_error_rolls_back_only_transition_and_preserves_original(database, kind):
    event_type, object_type, object_id, model, model_id = seed_guard_execution(database, kind)
    statements = []

    def fail_object_update(_conn, _cursor, statement, _params, _context, _many):
        if statement.startswith(f"UPDATE {model.__tablename__} SET"):
            statements.append(statement)
            raise RuntimeError("object transition unavailable")

    with database() as session:
        engine = session.get_bind()
    sa_event.listen(engine, "before_cursor_execute", fail_object_update)
    original = ValueError("original business failure")
    try:
        with pytest.raises(ValueError) as caught:
            with events.failure_guard(event_type, "actor", object_type, object_id, job_id="job", attempt=1):
                raise original
        assert caught.value is original
    finally:
        sa_event.remove(engine, "before_cursor_execute", fail_object_update)
    assert len(statements) == 1
    with database() as session:
        assert session.get(model, model_id).status == "running"
        assert session.get(Job, "job").status == "running"
        audits = list(session.scalars(select(AuditEvent)))
        assert len(audits) == 1
        assert audits[0].detail["job_id"] == "job" and audits[0].detail["attempt"] == 1
        assert len(list(session.scalars(select(EventOutbox)))) == 1


@pytest.mark.parametrize("kind", ["import", "collect"])
def test_failure_guard_from_old_attempt_cannot_fail_current_attempt_object(database, kind):
    event_type, object_type, object_id, model, model_id = seed_guard_execution(database, kind)
    with database() as session:
        session.get(Job, "job").attempts = 2
    for attempt in (1, 1, 2):
        with pytest.raises(RuntimeError):
            with events.failure_guard(event_type, "actor", object_type, object_id, job_id="job", attempt=attempt):
                raise RuntimeError("attempt failed")
        with database() as session:
            assert session.get(model, model_id).status == ("running" if attempt == 1 else "failed")
            audits = list(session.scalars(select(AuditEvent)))
            assert sorted(row.detail["attempt"] for row in audits) == ([1] if attempt == 1 else [1, 2])


def test_failure_recording_error_never_masks_original_or_logs_its_contents(monkeypatch, caplog):
    @contextmanager
    def broken_scope():
        raise RuntimeError("token=should-not-be-logged")
        yield  # pragma: no cover

    monkeypatch.setattr(events, "session_scope", broken_scope)
    original = ValueError("SQL and credentials must not escape into logs")
    with pytest.raises(ValueError) as caught:
        with events.failure_guard(EventType.COLLECT_FAILED, None, "datasource", "ds"):
            raise original
    assert caught.value is original
    assert "未能写入" in caplog.text and "should-not-be-logged" not in caplog.text
    assert str(original) not in caplog.text


def test_success_guard_does_not_create_failure_or_open_an_independent_transaction(monkeypatch):
    def unexpected():
        pytest.fail("successful operation opened a failure transaction")

    monkeypatch.setattr(events, "session_scope", unexpected)
    with events.failure_guard(EventType.COLLECT_FAILED, None, "datasource", "ds"):
        pass


def test_new_subscription_and_event_can_be_written_in_the_same_transaction(database):
    with database() as session:
        session.add(
            WebhookSubscription(
                name="new",
                url="https://example.invalid/hook",
                secret_enc="unused",
                event_types=[EventType.COLLECT_COMPLETED],
            )
        )
        audit.record(session, None, "collect", "datasource", "ds")
    with database() as session:
        assert session.scalar(select(EventOutbox)).subscription_id == session.scalar(select(WebhookSubscription)).id


def test_legacy_audit_defaults_and_safe_row_counts_are_preserved(database):
    with database() as session:
        session.add(AuditEvent(id="legacy", actor_name="system", action="legacy", object_type="system"))
        counted = audit.record(session, None, "analysis.explore", "analysis_run", "run", rows=4)
        counted_id = counted.id
    with database() as session:
        assert session.get(AuditEvent, "legacy").detail == {}
        assert session.get(AuditEvent, counted_id).detail == {"rows": 4}
        assert list(session.scalars(select(EventOutbox))) == []


def test_outbox_subscription_selection_protects_against_concurrent_delete(database):
    statements = []

    def capture(_conn, statement, _multiparams, _params, _options):
        statements.append(str(statement.compile(dialect=postgresql.dialect())))

    with database() as session:
        subscription(session)
        engine = session.get_bind()
        sa_event.listen(engine, "before_execute", capture)
        try:
            audit.record(session, None, "metric_version.approve", "metric_version", "v")
            session.flush()
        finally:
            sa_event.remove(engine, "before_execute", capture)
    queries = [sql for sql in statements if "FROM webhook_subscription" in sql]
    assert len(queries) == 1
    assert "FOR KEY SHARE" in queries[0] and "ORDER BY webhook_subscription.id" in queries[0]
    assert "enabled IS true" in queries[0]


def test_failure_event_requires_error_and_all_event_names_are_append_only(database):
    assert [item.value for item in EventType] == [
        "metric_version.submitted",
        "metric_version.published",
        "metric_version.rejected",
        "metric_version.affected",
        "metric_version.invalidated",
        "join_path.approved",
        "join_path.affected",
        "collect.completed",
        "collect.failed",
        "collect.schema_changed",
        "lineage_import.completed",
        "lineage_import.failed",
        "attribution.unavailable",
        "auth.login_failure_threshold",
        "webhook.test",
    ]
    with database() as session:
        row = audit.record(session, None, "collect", "datasource", "ds", run_id="r")
        payload = events.event_payload(events.event_from_audit(row))
    payload["outcome"] = "failure"
    with pytest.raises(ValidationError, match="require an error"):
        GovernanceEvent.model_validate(payload)
    assert payload["actor"] == "system" and payload["at"].endswith("Z")
