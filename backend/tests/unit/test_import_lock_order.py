"""复核与阶段二按相同顺序加锁，使用会话替身固定交错。"""

from __future__ import annotations

import pytest

from avelis.api import imports
from avelis.importer import batch
from avelis.importer.archive import ImportFile
from avelis.models import CandidateEdge, Datasource, ImportBatch, ImportJob, Job, LineageEdge
from tests.unit.test_import_batch_service import MemorySession, create
from tests.unit.test_import_batch_service import memory as memory  # noqa: F401
from tests.unit.test_import_reference_refresh import ReviewSession
from tests.unit.test_import_reference_refresh import referenced as referenced  # noqa: F401


def trace_locks(monkeypatch, review_session, before_worker_source_lock):
    locks, refreshes, workers = {}, [], []
    original_scalars = MemorySession.scalars
    original_refresh = batch.refresh_status

    def scalars(session, query):
        if query._for_update_arg is not None:
            entity = query.column_descriptions[0]["entity"]
            held = locks.setdefault(session, [])
            if entity is Datasource and session is not review_session and not workers:
                # worker 请求 D 时不能持有 B，复核才能先完成对 B 的计数刷新。
                assert ImportBatch not in held
                workers.append(session)
                before_worker_source_lock()
            if entity is ImportBatch:
                assert Datasource in held
            if entity is Job:
                assert Datasource not in held and ImportBatch not in held
            held.append(entity)
        return original_scalars(session, query)

    def refresh(session, imported, **kwargs):
        if session is review_session:
            assert Datasource in locks[session]
            refreshes.append(imported.id)
        return original_refresh(session, imported, **kwargs)

    monkeypatch.setattr(MemorySession, "scalars", scalars)
    monkeypatch.setattr(batch, "refresh_status", refresh)
    return locks, refreshes, workers


@pytest.mark.parametrize("execution_status", ["queued", "running"])
@pytest.mark.parametrize("action", ["single", "bulk", "manual"])
def test_review_refresh_completes_before_worker_locks_referencing_batch(
    referenced, monkeypatch, execution_status, action
):
    waiting, _ = batch.create_batch(
        referenced.session,
        referenced.actor,
        datasource_id=referenced.job.datasource_id,
        files=[
            ImportFile("shared.sql", "SELECT 1;", "sql"),
            ImportFile("new.sql", "CREATE TABLE dw.new_summary AS SELECT id FROM src.events;", "sql"),
        ],
    )
    waiting.status = execution_status
    execution = Job(id="execution", kind="import", status="running", attempts=1)
    referenced.session.add(execution)
    waiting.job_id = execution.id

    def review():
        if action == "single":
            for candidate in referenced.pending:
                imports.decide(
                    candidate.id,
                    imports.DecisionBody(decision="reject", reason="来源不符"),
                    user=referenced.actor,
                    session=referenced.session,
                )
        elif action == "bulk":
            imports.decide_many(
                imports.DecisionsBody(ids=[c.id for c in referenced.pending], decision="reject", reason="来源不符"),
                user=referenced.actor,
                session=referenced.session,
            )
        else:
            imports.manual_edge(
                referenced.owner.id,
                imports.ManualEdge(
                    file_id=referenced.job.id,
                    source_ref="default_catalog.src.events",
                    target_ref="default_catalog.dw.manual",
                    relation="table",
                    reason="补充来源",
                ),
                user=referenced.actor,
                session=referenced.session,
            )
        assert waiting.status == "running"
        assert waiting.stats["pending_candidates"] == (2 if action == "manual" else 0)

    locks, refreshes, workers = trace_locks(monkeypatch, referenced.session, review)

    result = batch.run_import(waiting.id, job_id=execution.id, attempt=1)

    assert len(workers) == 1
    assert locks[workers[0]][:3] == [Job, Datasource, ImportBatch]
    assert waiting.id in refreshes
    assert result["status"] == ("needs_review" if action == "manual" else "done")
    assert waiting.stats["pending_candidates"] == (2 if action == "manual" else 0)
    assert all(c.status == ("pending" if action == "manual" else "rejected") for c in referenced.pending)
    if action != "manual":
        assert all(
            c.decided_by == referenced.actor.id and c.reason == "来源不符" and c.decided_at for c in referenced.pending
        )
    else:
        assert any(
            e.import_job_id == referenced.job.id
            and e.target_ref == "default_catalog.dw.manual"
            and e.status == "active"
            for e in referenced.rows[LineageEdge]
        )


@pytest.mark.parametrize("decision", ["reject", "modify"])
def test_phase_two_reads_review_decision_after_source_lock(memory, monkeypatch, decision):
    first = create(memory, "CREATE TABLE dw.summary AS SELECT id FROM src.events;")
    batch.run_import(first.id)
    previous = memory.rows[CandidateEdge][0]
    second = create(memory, "CREATE TABLE dw.summary AS SELECT id FROM src.events;\nSELECT 1;")
    review_session = ReviewSession(memory.rows)
    monkeypatch.setattr(imports.service, "resolve_qualified", lambda *_: None)

    def review():
        imports.decide(
            previous.id,
            imports.DecisionBody(decision=decision, reason="核对来源", target_column="reviewed_id"),
            user=memory.actor,
            session=review_session,
        )

    locks, _, workers = trace_locks(monkeypatch, review_session, review)

    result = batch.run_import(second.id)

    assert result["status"] == "done"
    assert locks[workers[0]][:2] == [Datasource, ImportBatch]
    job = next(j for j in memory.rows[ImportJob] if j.batch_id == second.id)
    inherited = next(c for c in memory.rows[CandidateEdge] if c.job_id == job.id)
    assert inherited.status == ("rejected" if decision == "reject" else "modified")
    assert inherited.reason == "核对来源" and inherited.decided_by == memory.actor.id
    assert inherited.decided_at == previous.decided_at
    assert inherited.evidence["carried_decision_from"] == previous.id
    assert second.stats["invalidated_decisions"] == 0
    edge = memory.session.get(LineageEdge, inherited.lineage_edge_id)
    if decision == "reject":
        assert edge.status == "superseded"
    else:
        assert inherited.modified == {"target_column": "reviewed_id"}
        assert edge.status == "active" and edge.target_column == "reviewed_id"
