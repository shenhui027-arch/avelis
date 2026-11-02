"""回收先锁 Job，再收尾对象，并按持久执行尝试键检查失败事件。"""

from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from avelis import events, jobs
from avelis.models import CollectionRun, ImportBatch, Job


@pytest.fixture
def reclaim(monkeypatch, request):
    kind = request.param
    business = (
        CollectionRun(id="run", datasource_id="source", status="running")
        if kind == "collect"
        else ImportBatch(id="batch", status="running", progress={})
    )
    job = Job(
        id="job",
        kind=kind,
        status="failed",
        attempts=3,
        created_by="actor",
        error="timeout",
        payload={"datasource_id": "source"} if kind == "collect" else {"batch_id": business.id},
        result={"abandoned_governance": "pending", **({"run_id": business.id} if kind == "collect" else {})},
    )
    statements = []

    def scalar(statement):
        statements.append(statement)
        assert statement._for_update_arg is not None
        entity = statement.column_descriptions[0]["entity"]
        if entity is Job:
            return job
        assert entity is type(business)
        assert statement.compile().params["id_1"] == business.id
        return business

    session = SimpleNamespace(scalar=scalar)

    @contextmanager
    def scope():
        yield session

    def check(*_args):
        assert business.status == "failed"
        assert job.result["abandoned_governance"] == "done"
        return False

    recorded = Mock()
    exists = Mock(side_effect=check)
    monkeypatch.setattr(jobs, "session_scope", scope)
    monkeypatch.setattr(jobs, "utcnow", lambda: datetime(2026, 11, 3))
    monkeypatch.setattr(events, "failure_already_recorded", exists, raising=False)
    monkeypatch.setattr(events, "record_failure", recorded)
    return SimpleNamespace(
        job=job,
        business=business,
        session=session,
        statements=statements,
        exists=exists,
        recorded=recorded,
        event_type="collect.failed" if kind == "collect" else "lineage_import.failed",
        object_type="datasource" if kind == "collect" else "import_batch",
        object_id="source" if kind == "collect" else business.id,
    )


@pytest.mark.parametrize("reclaim", ["collect", "import"], indirect=True)
@pytest.mark.parametrize("already_recorded", [False, True])
def test_reclaim_transfers_state_but_does_not_repeat_attempt_failure(reclaim, already_recorded):
    state = reclaim
    if already_recorded:
        state.exists.side_effect = None
        state.exists.return_value = True
    # 传入旧扫描快照，关联和 attempt 必须取当前已加锁的 Job。
    jobs._record_abandoned_governance(state.job.id, "stale-kind", {}, {"run_id": "old-run"}, "timeout")
    assert [statement.column_descriptions[0]["entity"] for statement in state.statements] == [
        Job,
        type(state.business),
    ]
    assert state.business.status == "failed"
    assert state.job.result["abandoned_governance"] == "done"
    state.exists.assert_called_once_with(state.session, state.event_type, state.object_id, state.job.id, 3)
    if already_recorded:
        state.recorded.assert_not_called()
    else:
        state.recorded.assert_called_once_with(
            state.session,
            "actor",
            state.event_type,
            state.object_type,
            state.object_id,
            error_code="WORKER_ABANDONED",
            error_reason="后台任务运行超时",
            job_id="job",
            attempt=3,
        )


@pytest.mark.parametrize("reclaim", ["collect", "import"], indirect=True)
def test_pending_replay_and_different_attempts_use_current_dedupe_key(reclaim):
    state = reclaim
    recorded_keys = set()

    def exists(_session, event_type, object_id, job_id, attempt):
        return (event_type, object_id, job_id, attempt) in recorded_keys

    def record(_session, _actor_id, event_type, _object_type, object_id, **detail):
        recorded_keys.add((event_type, object_id, detail["job_id"], detail["attempt"]))

    state.exists.side_effect = exists
    state.recorded.side_effect = record
    for attempt in (3, 3, 4, 4):
        state.job.attempts = attempt
        state.job.result = {**state.job.result, "abandoned_governance": "pending"}
        state.business.status = "running"
        jobs._record_abandoned_governance(state.job.id, state.job.kind, {}, {}, "timeout")
    assert state.recorded.call_count == 2
    assert recorded_keys == {(state.event_type, state.object_id, "job", attempt) for attempt in (3, 4)}
