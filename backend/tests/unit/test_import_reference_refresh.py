"""候选处理和手工补边刷新所有引用批次，使用内存会话离线验证。"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from sqlalchemy.sql.selectable import Select

from avelis.api import imports
from avelis.importer import batch, service
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
    pending = [CandidateEdge(job_id=job.id, statement_id=statement.id, origin="model", status="pending",
                             source_ref="default_catalog.src.events", source_column=column,
                             target_ref="default_catalog.dw.summary", target_column=column,
                             relation="value_direct", evidence={}) for column in ("id", "value")]
    reused = [ImportBatch(id=f"reused-{index}", datasource_id=job.datasource_id, status="needs_review",
                          options={"duplicate_file_ids": [job.id, job.id]}, stats={}) for index in range(2)]
    unrelated = [ImportBatch(id="unrelated", datasource_id=job.datasource_id, status="needs_review",
                             options={}, stats={"untouched": True}),
                 ImportBatch(id="other-source", datasource_id="other-ds", status="needs_review",
                             options={"duplicate_file_ids": [job.id]}, stats={"untouched": True})]
    trace = LineageEdge(import_job_id=job.id, status="intermediate", origin="import",
                        source_ref="default_catalog.src.events", target_ref="default_catalog.dw.stage",
                        relation="table", attributes={})
    memory.session.add_all([*pending, *reused, *unrelated, trace])
    affected = [owner, *reused]
    for imported in affected:
        batch.refresh_status(memory.session, imported)
    untouched = [(item, copy.deepcopy(item.stats), item.status, item.updated_at) for item in unrelated]
    return SimpleNamespace(**vars(memory), owner=owner, job=job, pending=pending,
                           affected=affected, untouched=untouched)


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
        result = imports.decide(candidate.id, imports.DecisionBody(decision="accept", reason="人工确认"),
                                user=referenced.actor, session=referenced.session)
        assert result["id"] == referenced.owner.id
        assert_refreshed(referenced, pending=1 - index, active=1 + index)
    assert referenced.job.status == "done"


def test_bulk_decisions_refresh_every_referencing_batch(referenced):
    ids = [item.id for item in referenced.pending]
    result = imports.decide_many(imports.DecisionsBody(ids=ids, decision="accept", reason="批量确认"),
                                 user=referenced.actor, session=referenced.session)
    assert result == {"ids": ids, "decision": "accept", "processed": 2}
    assert_refreshed(referenced, pending=0, active=2)


def test_manual_edge_refreshes_owner_and_other_referencing_batches(referenced):
    result = imports.manual_edge(
        referenced.affected[1].id,
        imports.ManualEdge(file_id=referenced.job.id, source_ref="default_catalog.src.events",
                           target_ref="default_catalog.dw.manual", relation="table", reason="补充来源"),
        user=referenced.actor, session=referenced.session)
    assert result["id"] == referenced.affected[1].id
    assert_refreshed(referenced, pending=2, active=1)


def test_decision_refreshes_references_when_file_has_no_owner(referenced):
    referenced.job.batch_id = None
    referenced.affected = referenced.affected[1:]
    for candidate in referenced.pending:
        service.decide(referenced.session, referenced.actor, candidate, "reject", "来源不符", {})
    assert_refreshed(referenced, pending=0, active=0)
