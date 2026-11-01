"""候选处理和手工补边刷新所有引用批次，使用内存会话离线验证。"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from sqlalchemy.sql.selectable import Select

from avelis.api import imports
from avelis.importer import batch, service
from avelis.importer.archive import ImportFile
from avelis.models import CandidateEdge, ExtractedStatement, ImportBatch, ImportJob, LineageEdge
from tests.unit.test_import_batch_service import MemorySession, create
from tests.unit.test_import_batch_service import memory as memory


class ReviewSession(MemorySession):
    def get(self, entity, object_id, **_kwargs):
        return super().get(entity, object_id)

    def execute(self, statement):
        if isinstance(statement, Select):
            return SimpleNamespace(all=lambda: list(self.scalars(statement)))
        return super().execute(statement)


@pytest.fixture
def referenced(memory, monkeypatch):
    memory.session = ReviewSession(memory.rows)
    monkeypatch.setattr(service, "resolve_qualified", lambda *_: None)
    owner = create(memory, "SELECT 1;")
    batch.run_import(owner.id)
    job = memory.rows[ImportJob][0]
    statement = memory.rows[ExtractedStatement][0]
    pending = [
        CandidateEdge(
            job_id=job.id,
            statement_id=statement.id,
            origin="model",
            status="pending",
            source_ref="default_catalog.src.events",
            source_column=column,
            target_ref="default_catalog.dw.summary",
            target_column=column,
            relation="value_direct",
            evidence={},
        )
        for column in ("id", "value")
    ]
    reused = [
        ImportBatch(
            id=f"reused-{index}",
            datasource_id=job.datasource_id,
            status="needs_review",
            options={"duplicate_file_ids": [job.id, job.id]},
            stats={},
        )
        for index in range(2)
    ]
    unrelated = [
        ImportBatch(
            id="unrelated",
            datasource_id=job.datasource_id,
            status="needs_review",
            options={},
            stats={"untouched": True},
        ),
        ImportBatch(
            id="other-source",
            datasource_id="other-ds",
            status="needs_review",
            options={"duplicate_file_ids": [job.id]},
            stats={"untouched": True},
        ),
    ]
    trace = LineageEdge(
        import_job_id=job.id,
        status="intermediate",
        origin="import",
        source_ref="default_catalog.src.events",
        target_ref="default_catalog.dw.stage",
        relation="table",
        attributes={},
    )
    memory.session.add_all([*pending, *reused, *unrelated, trace])
    affected = [owner, *reused]
    for imported in affected:
        batch.refresh_status(memory.session, imported)
    untouched = [(item, copy.deepcopy(item.stats), item.status, item.updated_at) for item in unrelated]
    return SimpleNamespace(
        **vars(memory), owner=owner, job=job, pending=pending, affected=affected, untouched=untouched
    )


def assert_refreshed(referenced, *, pending, active):
    for imported in referenced.affected:
        assert imported.stats["pending_candidates"] == pending
        assert imported.stats["active_edges"] == active
        assert imported.stats["intermediate_edges"] == 1
        assert imported.stats["edges"] == active + 1
        assert imported.stats["statement_count"] == 1
        assert imported.status == ("needs_review" if pending else "done")
    for item, stats, status, updated_at in referenced.untouched:
        assert (item.stats, item.status, item.updated_at) == (stats, status, updated_at)


def test_single_decision_refreshes_owner_and_every_referencing_batch(referenced):
    for index, candidate in enumerate(referenced.pending):
        result = imports.decide(
            candidate.id,
            imports.DecisionBody(decision="accept", reason="人工确认"),
            user=referenced.actor,
            session=referenced.session,
        )
        assert result["id"] == referenced.owner.id
        assert_refreshed(referenced, pending=1 - index, active=1 + index)
    assert referenced.job.status == "done"


def test_bulk_decisions_refresh_every_referencing_batch(referenced):
    ids = [item.id for item in referenced.pending]
    result = imports.decide_many(
        imports.DecisionsBody(ids=ids, decision="accept", reason="批量确认"),
        user=referenced.actor,
        session=referenced.session,
    )
    assert result == {"ids": ids, "decision": "accept", "processed": 2}
    assert_refreshed(referenced, pending=0, active=2)


def test_manual_edge_refreshes_owner_and_other_referencing_batches(referenced):
    result = imports.manual_edge(
        referenced.affected[1].id,
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
    assert result["id"] == referenced.affected[1].id
    assert_refreshed(referenced, pending=2, active=1)


def test_decision_refreshes_references_when_file_has_no_owner(referenced):
    referenced.job.batch_id = None
    referenced.affected = referenced.affected[1:]
    for candidate in referenced.pending:
        service.decide(referenced.session, referenced.actor, candidate, "reject", "来源不符", {})
    assert_refreshed(referenced, pending=0, active=0)


@pytest.mark.parametrize("execution_status", ["queued", "running"])
@pytest.mark.parametrize("action", ["decision", "manual"])
def test_reference_refresh_preserves_execution_and_staging(referenced, execution_status, action):
    waiting, created = batch.create_batch(
        referenced.session,
        referenced.actor,
        datasource_id=referenced.job.datasource_id,
        files=[
            ImportFile("shared.sql", "SELECT 1;", "sql"),
            ImportFile("new.sql", "CREATE TABLE dw.new_summary AS SELECT id FROM src.events;", "sql"),
        ],
    )
    assert created and waiting.options["duplicate_file_ids"] == [referenced.job.id]
    staged = next(j for j in referenced.rows[ImportJob] if j.batch_id == waiting.id)
    waiting.status = execution_status
    progress = copy.deepcopy(waiting.progress)
    if action == "decision":
        for candidate in referenced.pending:
            service.decide(referenced.session, referenced.actor, candidate, "reject", "来源不符", {})
        pending, active = 0, 0
    else:
        service.add_manual_edge(
            referenced.session,
            referenced.actor,
            referenced.job,
            {
                "source_ref": "default_catalog.src.events",
                "target_ref": "default_catalog.dw.manual",
                "relation": "table",
            },
            "补充来源",
        )
        pending, active = 2, 1

    assert_refreshed(referenced, pending=pending, active=active)
    assert waiting.status == execution_status and waiting.progress == progress
    assert waiting.stats["pending_candidates"] == pending
    assert waiting.stats["active_edges"] == active
    assert waiting.stats["statement_count"] == 1
    assert staged.status == "superseded" and staged.stats["staged"]
    assert not [s for s in referenced.rows[ExtractedStatement] if s.job_id == staged.id]

    result = batch.run_import(waiting.id)

    assert result["status"] == ("needs_review" if pending else "done")
    assert staged.status == "done" and not staged.stats.get("staged")
    assert [s for s in referenced.rows[ExtractedStatement] if s.job_id == staged.id]
    assert any(
        e.import_job_id == staged.id and e.status == "active" and e.target_ref == "default_catalog.dw.new_summary"
        for e in referenced.rows[LineageEdge]
    )
