"""批次服务使用内存会话验证，不连接元数据库或模型。"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BooleanClauseList

from avelis.importer import batch, service
from avelis.importer.archive import ImportFile
from avelis.models import (
    AuditEvent,
    CandidateEdge,
    Datasource,
    ExtractedStatement,
    ImportBatch,
    ImportJob,
    ImportSource,
    Job,
    LineageEdge,
    User,
)

SCHEMA = {"default_catalog": {"src": {"events": {"id": "INT", "value": "INT"}}}}


def matches(obj, criterion):
    if isinstance(criterion, BooleanClauseList):
        return all(matches(obj, c) for c in criterion.clauses)
    left = getattr(obj, criterion.left.key)
    right = criterion.right.value
    if criterion.operator is operators.eq:
        return left == right
    if criterion.operator is operators.ne:
        return left != right
    if criterion.operator is operators.in_op:
        return left in right
    raise AssertionError(f"测试会话不支持：{criterion}")


class MemorySession:
    def __init__(self, rows):
        self.rows = rows
        self.flush_count = 0

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = batch.new_id()
        for name in ("created_at", "updated_at"):
            if hasattr(type(obj), name) and getattr(obj, name) is None:
                setattr(obj, name, datetime.now(UTC))
        for name, value in (
            ("stats", {}),
            ("options", {}),
            ("params", {}),
            ("evidence", {}),
            ("attributes", {}),
            ("modified", {}),
            ("model_payload", []),
        ):
            if hasattr(type(obj), name) and getattr(obj, name) is None:
                setattr(obj, name, copy.deepcopy(value))
        table = self.rows.setdefault(type(obj), [])
        if obj not in table:
            table.append(obj)

    def add_all(self, objects):
        for obj in objects:
            self.add(obj)

    def flush(self):
        self.flush_count += 1

    def get(self, entity, object_id):
        return next((o for o in self.rows.get(entity, []) if o.id == object_id), None)

    def scalars(self, query):
        description = query.column_descriptions[0]
        objects = list(self.rows.get(description["entity"], []))
        for criterion in query._where_criteria:
            objects = [obj for obj in objects if matches(obj, criterion)]
        if description["name"] != description["entity"].__name__:
            return [getattr(obj, description["name"]) for obj in objects]
        return objects

    def scalar(self, query):
        return next(iter(self.scalars(query)), None)

    def execute(self, statement):
        entity = LineageEdge if statement.table.name == "lineage_edge" else CandidateEdge
        count = 0
        for obj in self.rows.get(entity, []):
            if all(matches(obj, c) for c in statement._where_criteria):
                for key, value in statement._values.items():
                    setattr(obj, key if isinstance(key, str) else key.key, value.value)
                count += 1
        return SimpleNamespace(rowcount=count)


@pytest.fixture
def memory(monkeypatch):
    rows = {}
    session = MemorySession(rows)
    actor = User(id="actor", username="reviewer", display_name="维护者", password_hash="x")
    ds = Datasource(id="ds", name="测试源", kind="starrocks", options={})
    session.add_all([actor, ds])
    transactions = []

    @contextmanager
    def scope():
        snapshot = copy.deepcopy(rows)
        tx = MemorySession(rows)
        transactions.append(tx)
        try:
            yield tx
        except Exception:
            rows.clear()
            rows.update(snapshot)
            raise

    monkeypatch.setattr(batch, "session_scope", scope)
    monkeypatch.setattr(batch, "build_schema", lambda *_: copy.deepcopy(SCHEMA))
    monkeypatch.setattr(
        batch,
        "get_settings",
        lambda: SimpleNamespace(import_max_bytes=20 * 1024**2, import_max_batch_bytes=100 * 1024**2),
    )
    monkeypatch.setattr(service, "require_import_access", lambda *_: None)
    monkeypatch.setattr(batch.modelcfg, "policy", lambda *_: {"send_code": False})
    monkeypatch.setattr(batch.modelcfg, "resolve", lambda *_: pytest.fail("离线测试不能请求真实模型"))
    return SimpleNamespace(session=session, rows=rows, actor=actor, transactions=transactions)


def create(memory, content, filename="pipeline.sql", **options):
    return batch.create_batch(
        memory.session, memory.actor, datasource_id="ds", files=[ImportFile(filename, content, "sql")], **options
    )[0]


def candidates(memory, job_id):
    return [c for c in memory.rows.get(CandidateEdge, []) if c.job_id == job_id]


def test_assistance_uses_persisted_database_after_use_for_unrecognized_candidate(memory, monkeypatch):
    from tests.unit.test_import_assist import FakeImportModel, edge

    source = 'spark.sql("USE a")\nspark.sql("USE b")\ndf = spark.table("events")\ndf.write.saveAsTable("summary")'
    fake = FakeImportModel({"edges": [edge(source_table="events", target_table="summary", line=4)]})
    monkeypatch.setattr(batch, "build_schema", lambda *_: {"default_catalog": {"b": {"events": {"id": "INT"}}}})
    monkeypatch.setattr(batch.modelcfg, "policy", lambda *_: {"send_code": True})
    monkeypatch.setattr(batch.modelcfg, "resolve", lambda *_: batch.modelcfg.Resolved(fake, "test:fake"))
    imported, _ = batch.create_batch(
        memory.session,
        memory.actor,
        datasource_id="ds",
        default_db="fallback",
        files=[ImportFile("pipeline.py", source, "python")],
    )
    batch.run_import(imported.id)
    job = memory.rows[ImportJob][0]
    context = job.stats["statement_contexts"][-1]
    assert context["seq"] == 3 and context["default_db"] == "b"
    assert len(fake.prompts) == 1 and "默认数据库为 b" in fake.prompts[0]
    assert fake.prompts[0] == job.model_payload[0]["prompt"]
    candidate = candidates(memory, job.id)[0]
    assert (candidate.source_ref, candidate.target_ref) == ("default_catalog.b.events", "default_catalog.b.summary")


def test_assistance_restored_sql_uses_operation_database_inside_block_with_use(memory, monkeypatch):
    from tests.unit.test_import_assist import FakeImportModel, operation

    sql = "INSERT INTO summary (id) SELECT id FROM events"
    source = f'spark.sql("USE a")\nspark.sql("USE b")\nrun_sql("{sql}")'
    fake = FakeImportModel({"operations": [operation(sql=sql, start=3, end=3)]})
    monkeypatch.setattr(batch, "build_schema", lambda *_: {"default_catalog": {"b": {"events": {"id": "INT"}}}})
    monkeypatch.setattr(batch.modelcfg, "policy", lambda *_: {"send_code": True})
    monkeypatch.setattr(batch.modelcfg, "resolve", lambda *_: batch.modelcfg.Resolved(fake, "test:fake"))
    imported, _ = batch.create_batch(
        memory.session,
        memory.actor,
        datasource_id="ds",
        default_db="fallback",
        files=[ImportFile("pipeline.py", source, "python")],
        model_assist={"code": True},
    )
    batch.run_import(imported.id)
    job = memory.rows[ImportJob][0]
    assert "第 2 行之后默认数据库为 b" in fake.prompts[0]
    assert fake.prompts[0] == job.model_payload[0]["prompt"]
    candidate = candidates(memory, job.id)[0]
    assert (candidate.source_ref, candidate.target_ref) == ("default_catalog.b.events", "default_catalog.b.summary")
    assert candidate.origin == "model_sql" and candidate.status == "pending"


def test_assistance_database_context_preserves_same_line_statement_order(memory, monkeypatch):
    from tests.unit.test_import_assist import FakeImportModel, edge

    source = 'spark.sql("USE a; USE b; ANALYZE events")'
    fake = FakeImportModel({"edges": [edge(source_table="events", target_table="events")]})
    monkeypatch.setattr(batch, "build_schema", lambda *_: {"default_catalog": {"b": {"events": {"id": "INT"}}}})
    monkeypatch.setattr(batch.modelcfg, "policy", lambda *_: {"send_code": True})
    monkeypatch.setattr(batch.modelcfg, "resolve", lambda *_: batch.modelcfg.Resolved(fake, "test:fake"))
    imported, _ = batch.create_batch(
        memory.session,
        memory.actor,
        datasource_id="ds",
        default_db="fallback",
        files=[ImportFile("pipeline.py", source, "python")],
        model_assist={"failed_sql": True},
    )
    batch.run_import(imported.id)
    job = memory.rows[ImportJob][0]
    assert [c["default_db"] for c in job.stats["statement_contexts"]] == ["fallback", "a", "b"]
    assert len(fake.prompts) == 1 and "默认数据库为 b" in fake.prompts[0]
    assert candidates(memory, job.id)[0].source_ref == "default_catalog.b.events"


def test_batch_staging_does_not_supersede_current_before_phase_two(memory):
    first = create(memory, "CREATE TABLE dw.summary AS SELECT id FROM src.events;")
    batch.run_import(first.id)
    old = next(j for j in memory.rows[ImportJob] if j.batch_id == first.id)
    old_edge = memory.rows[LineageEdge][0]
    second = create(memory, "CREATE TABLE dw.summary AS SELECT id, value FROM src.events;")
    staged = next(j for j in memory.rows[ImportJob] if j.batch_id == second.id)
    assert old.status == "done" and old_edge.status == "active"
    assert staged.status == "superseded" and staged.stats["staged"]
    assert batch.batch_view(memory.session, second)["files"][0]["status"] == "queued"
    batch.run_import(second.id)
    assert old.status == "superseded" and old.superseded_by == staged.id
    assert old_edge.status == "superseded" and old_edge.superseded_at
    assert staged.revision == 2 and staged.status == "done"
    assert second.stats["replaced_edges"] == 1


def test_identical_current_content_returns_original_batch_across_filenames(memory):
    content = "CREATE TABLE dw.summary AS SELECT id FROM src.events;"
    first = create(memory, content)
    batch.run_import(first.id)
    repeated, created = batch.create_batch(
        memory.session, memory.actor, datasource_id="ds", files=[ImportFile("renamed.sql", content, "sql")]
    )
    assert repeated.id == first.id and not created
    assert len(memory.rows[ImportJob]) == 1


def test_equal_content_queued_before_first_finishes_is_deduplicated_under_phase_two_lock(memory):
    content = "CREATE TABLE dw.summary AS SELECT id FROM src.events;"
    first = create(memory, content)
    concurrent = create(memory, content)
    batch.run_import(first.id)
    batch.run_import(concurrent.id)
    first_job, reserved = memory.rows[ImportJob]
    assert first_job.revision == 1 and first_job.status == "done"
    assert reserved.stats["duplicate_of"] == first_job.id and reserved.status == "superseded"
    assert len(memory.rows[LineageEdge]) == 1
    view = batch.batch_view(memory.session, concurrent)
    assert [f["id"] for f in view["files"]] == [first_job.id]
    assert concurrent.stats["statement_count"] == 1


def test_reimport_inherits_rejected_edge_only_when_text_and_signature_match(memory):
    content = "CREATE TABLE dw.summary AS SELECT id FROM src.events;"
    first = create(memory, content)
    batch.run_import(first.id)
    old_job = next(j for j in memory.rows[ImportJob] if j.batch_id == first.id)
    previous = candidates(memory, old_job.id)[0]
    previous.status, previous.decided_by, previous.reason = "rejected", memory.actor.id, "来源不符"
    previous.decided_at = datetime.now(UTC)
    new = create(memory, content + "\nSELECT 1;")
    batch.run_import(new.id)
    new_job = next(j for j in memory.rows[ImportJob] if j.batch_id == new.id)
    inherited = candidates(memory, new_job.id)[0]
    assert inherited.status == "rejected"
    assert memory.session.get(LineageEdge, inherited.lineage_edge_id).status == "superseded"
    assert inherited.reason == "来源不符" and inherited.evidence["carried_decision_from"] == previous.id
    assert new.stats["invalidated_decisions"] == 0


def test_changed_statement_invalidates_previous_decision(memory):
    first = create(memory, "CREATE TABLE dw.summary AS SELECT id FROM src.events;")
    batch.run_import(first.id)
    previous = memory.rows[CandidateEdge][0]
    previous.status, previous.decided_by = "rejected", memory.actor.id
    previous.decided_at = datetime.now(UTC)
    new = create(memory, "CREATE TABLE dw.summary AS SELECT value FROM src.events;")
    batch.run_import(new.id)
    assert new.stats["invalidated_decisions"] == 1
    assert memory.rows[CandidateEdge][-1].status == "written"


def test_phase_two_failure_rolls_back_replacement_and_new_results(memory, monkeypatch):
    first = create(memory, "CREATE TABLE dw.summary AS SELECT id FROM src.events;")
    batch.run_import(first.id)
    previous_id = memory.rows[ImportJob][0].id
    edge_id = memory.rows[LineageEdge][0].id
    new = create(memory, "CREATE TABLE dw.summary AS SELECT value FROM src.events;")
    before_count = len(memory.rows[ExtractedStatement])

    def fail(*_args, **_kwargs):
        raise RuntimeError("写入中断")

    monkeypatch.setattr(batch, "_persist_edge", fail)
    with pytest.raises(RuntimeError, match="写入中断"):
        batch.run_import(new.id)
    assert memory.session.get(ImportJob, previous_id).status == "done"
    assert memory.session.get(LineageEdge, edge_id).status == "active"
    assert len(memory.rows[ExtractedStatement]) == before_count
    assert memory.session.get(ImportBatch, new.id).status == "failed"


def test_params_create_whole_batch_revision_and_keep_previous_history(memory):
    original = create(memory, "CREATE TABLE dw.${target} AS SELECT id FROM src.events;")
    batch.run_import(original.id)
    assert original.status == "needs_params"
    rewritten = batch.set_params(memory.session, memory.actor, original, {"target": "summary"})
    assert rewritten.id != original.id and rewritten.status == "queued"
    batch.run_import(rewritten.id)
    assert rewritten.status == "done"
    old, current = memory.rows[ImportJob]
    assert old.status == "superseded" and current.revision == 2
    assert len(memory.rows[ImportSource]) == 2
    assert old.params == {} and current.params == {"target": "summary"}


def test_cross_file_dependency_sorting_and_folding_use_one_shared_schema(memory):
    imported, _ = batch.create_batch(
        memory.session,
        memory.actor,
        datasource_id="ds",
        files=[
            ImportFile("second.sql", "CREATE TABLE dw.summary AS SELECT id FROM dw.stage;", "sql"),
            ImportFile("first.sql", "CREATE TEMPORARY TABLE dw.stage AS SELECT id FROM src.events;", "sql"),
        ],
    )
    batch.run_import(imported.id)
    active = [e for e in memory.rows[LineageEdge] if e.status == "active"]
    intermediate = [e for e in memory.rows[LineageEdge] if e.status == "intermediate"]
    assert [(e.source_ref, e.target_ref) for e in active] == [
        ("default_catalog.src.events", "default_catalog.dw.summary")
    ]
    assert len(intermediate) == 2
    assert active[0].attributes["via"] == ["default_catalog.dw.stage"]
    assert set(active[0].attributes["hops"]) == {e.id for e in intermediate}
    assert all(
        c.status == "intermediate"
        for c in memory.rows[CandidateEdge]
        if c.lineage_edge_id in {e.id for e in intermediate}
    )


@pytest.mark.parametrize("change_producer", [False, True])
def test_duplicate_file_participates_in_cross_file_folding_without_new_file_revision(memory, change_producer):
    producer = "CREATE TEMPORARY TABLE dw.stage AS SELECT id FROM src.events;"
    consumer = "CREATE TABLE dw.summary AS SELECT id FROM dw.stage;"
    first, _ = batch.create_batch(
        memory.session,
        memory.actor,
        datasource_id="ds",
        files=[ImportFile("first.sql", producer, "sql"), ImportFile("second.sql", consumer, "sql")],
    )
    batch.run_import(first.id)
    first_jobs = {j.filename: j for j in memory.rows[ImportJob]}
    retained = {
        e.id: {c.name: copy.deepcopy(getattr(e, c.name)) for c in LineageEdge.__table__.columns}
        for e in memory.rows[LineageEdge]
        if e.status == "intermediate"
    }
    second, _ = batch.create_batch(
        memory.session,
        memory.actor,
        datasource_id="ds",
        files=[
            ImportFile("second.sql", consumer if change_producer else consumer + "SELECT 1;", "sql"),
            ImportFile("first.sql", producer + "SELECT 1;" if change_producer else producer, "sql"),
        ],
    )
    batch.run_import(second.id)
    active = [e for e in memory.rows[LineageEdge] if e.status == "active"]
    assert len(active) == 1
    assert active[0].source_ref == "default_catalog.src.events"
    assert active[0].target_ref == "default_catalog.dw.summary"
    assert active[0].attributes["via"] == ["default_catalog.dw.stage"]
    for hop in active[0].attributes["hops"]:
        assert memory.session.get(LineageEdge, hop).status == "intermediate"
    for edge_id, original in retained.items():
        edge = memory.session.get(LineageEdge, edge_id)
        assert {c.name: getattr(edge, c.name) for c in LineageEdge.__table__.columns} == original
    assert all(memory.session.get(LineageEdge, hop) for path in active[0].attributes["paths"] for hop in path)
    reused = first_jobs["second.sql" if change_producer else "first.sql"]
    assert reused.revision == 1 and reused.superseded_by is None
    assert len(memory.rows[ImportJob]) == 3


def test_reused_statement_and_file_counts_follow_current_batch_context(memory, monkeypatch):
    consumer = "CREATE TABLE dw.summary AS SELECT * FROM dw.stage;"
    first = create(memory, consumer, filename="consumer.sql")
    batch.run_import(first.id)
    job = memory.rows[ImportJob][0]
    stmt = memory.rows[ExtractedStatement][0]
    assert stmt.status == "partial" and stmt.reason
    old_reason = stmt.reason
    original_id, original_hash = stmt.id, stmt.text_hash
    assert job.stats["statements"] == {"partial": 1}
    second, _ = batch.create_batch(
        memory.session,
        memory.actor,
        datasource_id="ds",
        files=[
            ImportFile("consumer.sql", consumer, "sql"),
            ImportFile("producer.sql", "CREATE TEMPORARY TABLE dw.stage AS SELECT id, value FROM src.events;", "sql"),
        ],
    )
    phase_two = []

    def inspect_persisted(*_args, **_kwargs):
        phase_two.append((stmt.status, copy.deepcopy(job.stats["statements"])))

    monkeypatch.setattr(batch, "_assist", inspect_persisted)
    batch.run_import(second.id)
    assert phase_two == [("parsed", {"parsed": 1})]
    assert stmt.id == original_id and stmt.text_hash == original_hash
    assert stmt.status == "parsed" and stmt.reason != old_reason
    assert stmt.reason == "目标表未采集，按 SELECT 输出列名对应目标列"
    assert stmt.sql_text == consumer.rstrip(";") and stmt.statement_type == "CREATE TABLE AS"
    assert stmt.target_ref == "default_catalog.dw.summary"
    assert stmt.writes[0]["target_columns"] == ["id", "value"]
    assert len(memory.rows[ExtractedStatement]) == 2 and len(memory.rows[ImportJob]) == 2
    assert batch.batch_view(memory.session, second)["files"][0]["statements"] == {"parsed": 1}
    assert second.stats["statements"] == {"parsed": 2}


@pytest.mark.parametrize("repeat_duplicate", [False, True])
def test_progress_counts_reused_files_once_at_every_phase(memory, monkeypatch, repeat_duplicate):
    content = "CREATE TABLE dw.stage AS SELECT id FROM src.events;"
    first = create(memory, content, filename="producer.sql")
    batch.run_import(first.id)
    files = [
        ImportFile("producer.sql", content, "sql"),
        ImportFile("consumer.sql", "CREATE TABLE dw.summary AS SELECT id FROM dw.stage;", "sql"),
    ]
    if repeat_duplicate:
        files.append(ImportFile("copy.sql", content, "sql"))
    second, _ = batch.create_batch(memory.session, memory.actor, datasource_id="ds", files=files)
    assert second.progress["files_total"] == 2 and second.progress["files_done"] == 0
    progress = []
    update_progress = batch._progress

    def capture(batch_id, **values):
        update_progress(batch_id, **values)
        if batch_id == second.id:
            progress.append(dict(second.progress))

    monkeypatch.setattr(batch, "_progress", capture)
    batch.run_import(second.id)
    assert progress and all(p["files_total"] == 2 and p["files_done"] <= 2 for p in progress)
    assert second.progress["files_total"] == second.progress["files_done"] == 2


def test_parse_failure_reason_uses_statement_location_in_file(memory):
    imported = create(memory, "SELECT 1;\n\nINSERT INTO dw.daily_summary SELECT id FROM WHERE id=1;")
    batch.run_import(imported.id)
    stmt = next(s for s in memory.rows[ExtractedStatement] if s.status == "failed")
    assert stmt.line_start == 3
    assert stmt.reason == "SQL 无法解析：第 3 行第 49 列附近（WHERE）：Expected table name"


def test_reimport_retains_intermediate_rows_and_replaces_only_active_edges(memory):
    content = (
        "CREATE TEMPORARY TABLE dw.stage AS SELECT id FROM src.events;"
        "CREATE TABLE dw.summary AS SELECT id FROM dw.stage;"
    )
    first = create(memory, content)
    batch.run_import(first.id)
    previous_job = memory.rows[ImportJob][0]
    retained = {
        e.id: {c.name: copy.deepcopy(getattr(e, c.name)) for c in LineageEdge.__table__.columns}
        for e in memory.rows[LineageEdge]
        if e.status == "intermediate"
    }
    old_active = next(e for e in memory.rows[LineageEdge] if e.status == "active")
    second = create(memory, content + "SELECT 1;")
    batch.run_import(second.id)
    assert previous_job.status == "superseded"
    assert old_active.status == "superseded" and old_active.superseded_at
    assert second.stats["replaced_edges"] == 1
    assert len([e for e in memory.rows[LineageEdge] if e.status == "active"]) == 1
    for edge_id, original in retained.items():
        edge = memory.session.get(LineageEdge, edge_id)
        assert {c.name: getattr(edge, c.name) for c in LineageEdge.__table__.columns} == original
    assert second.stats["invalidated_decisions"] == 0


def test_reused_consumer_remaps_every_folded_path_when_active_becomes_intermediate(memory):
    consumer = "CREATE TABLE dw.summary AS SELECT id, value FROM dw.stage;"
    first = create(memory, consumer, filename="consumer.sql")
    batch.run_import(first.id)
    old_active = list(memory.rows[LineageEdge])
    assert old_active and all(e.status == "active" for e in old_active)
    producer = "CREATE TEMPORARY TABLE dw.stage AS SELECT id, value FROM src.events WHERE value > 0;"
    second, _ = batch.create_batch(
        memory.session,
        memory.actor,
        datasource_id="ds",
        files=[ImportFile("consumer.sql", consumer, "sql"), ImportFile("producer.sql", producer, "sql")],
    )
    batch.run_import(second.id)
    assert all(e.status == "superseded" for e in old_active)
    active = [e for e in memory.rows[LineageEdge] if e.status == "active"]
    influence = next(e for e in active if e.relation == "filter")
    assert len(influence.attributes["paths"]) == 2
    for edge in active:
        assert edge.attributes["hops"] == edge.attributes["paths"][0]
        for path in edge.attributes["paths"]:
            assert all(memory.session.get(LineageEdge, hop).status == "intermediate" for hop in path)
    assert all(
        c.status == "intermediate"
        for c in memory.rows[CandidateEdge]
        if memory.session.get(LineageEdge, c.lineage_edge_id).status == "intermediate"
    )
    assert second.stats["files"] == 2 and len(memory.rows[ImportJob]) == 2


def test_duplicate_direct_expression_keeps_every_path_queryable(memory):
    imported = create(memory, "CREATE TABLE dw.summary AS SELECT id + id AS total FROM src.events;")
    batch.run_import(imported.id)
    active = [e for e in memory.rows[LineageEdge] if e.status == "active"]
    assert len(active) == 1 and len(active[0].attributes["paths"]) == 2
    for path in active[0].attributes["paths"]:
        for hop in path:
            assert memory.session.get(LineageEdge, hop) is not None
    intermediate = [e for e in memory.rows[LineageEdge] if e.status == "intermediate"]
    assert len(intermediate) == 1
    evidence = next(c for c in memory.rows[CandidateEdge] if c.lineage_edge_id == intermediate[0].id)
    assert evidence.status == "intermediate"


@pytest.mark.parametrize("decision", ["reject", "modify"])
def test_folded_decision_is_inherited_by_normalized_statement_and_signature(memory, monkeypatch, decision):
    monkeypatch.setattr(service, "resolve_qualified", lambda *_: None)
    content = (
        "CREATE TEMPORARY TABLE dw.stage AS SELECT id FROM src.events;"
        "CREATE TABLE dw.summary AS SELECT id FROM dw.stage;"
    )
    first = create(memory, content)
    batch.run_import(first.id)
    previous = next(c for c in memory.rows[CandidateEdge] if c.origin == "folded")
    service.decide(memory.session, memory.actor, previous, decision, "确认折叠结果", {"source_column": "value"})
    second = create(memory, content.lower() + "SELECT 1;")
    batch.run_import(second.id)
    active = [e for e in memory.rows[LineageEdge] if e.status == "active"]
    current_job = next(j for j in memory.rows[ImportJob] if j.batch_id == second.id)
    inherited = next(c for c in candidates(memory, current_job.id) if c.origin == "folded")
    assert inherited.status == ("rejected" if decision == "reject" else "modified")
    assert inherited.evidence["carried_decision_from"] == previous.id
    assert inherited.reason == "确认折叠结果" and current_job.stats["inherited_decisions"] == 1
    assert second.stats["invalidated_decisions"] == 0
    if decision == "reject":
        assert active == []
    else:
        assert len(active) == 1 and active[0].source_column == "value"
        assert active[0].attributes["paths"] == inherited.evidence["paths"]
    assert all(e.source_column == "id" for e in memory.rows[LineageEdge] if e.status == "intermediate")


def test_dependency_prescan_substitutes_supplied_params_and_rename_targets(memory):
    imported, _ = batch.create_batch(
        memory.session,
        memory.actor,
        datasource_id="ds",
        params={"schema": "dw"},
        files=[
            ImportFile("consumer.sql", "CREATE TABLE dw.summary AS SELECT id FROM ${schema}.renamed;", "sql"),
            ImportFile("rename.sql", "ALTER TABLE dw.stage RENAME TO dw.renamed;", "sql"),
            ImportFile("producer.sql", "CREATE TEMPORARY TABLE dw.stage AS SELECT id FROM src.events;", "sql"),
        ],
    )
    batch.run_import(imported.id)
    active = [e for e in memory.rows[LineageEdge] if e.status == "active"]
    assert len(active) == 1 and active[0].source_ref == "default_catalog.src.events"
    assert active[0].target_ref == "default_catalog.dw.summary"


def test_statement_size_limit_is_failure_without_parsing(memory):
    imported = create(memory, "SELECT '" + "x" * batch.MAX_STATEMENT_BYTES + "';")
    batch.run_import(imported.id)
    stmt = memory.rows[ExtractedStatement][0]
    assert stmt.status == "failed" and stmt.reason == "语句过大（超过 1 MB）"
    assert not memory.rows.get(LineageEdge)


def test_progress_transactions_are_separate_and_throttled(memory):
    imported = create(memory, "SELECT 1;\n" * 401)
    batch.run_import(imported.id)
    assert imported.progress["statements_done"] == 401
    assert imported.progress["phase"] == "done"
    # 加上加载、阶段二、模型与最终状态事务，仍不应每个语句开一个事务。
    assert 6 <= len(memory.transactions) < 15


def test_normalized_hash_and_signature_are_stable_but_expression_sensitive():
    assert batch.normalized_text_hash("select id from src.events", "postgres") == batch.normalized_text_hash(
        "SELECT id\nFROM src.events", "postgres"
    )
    assert batch.normalized_text_hash("SELECT id FROM src.events", "postgres") != batch.normalized_text_hash(
        "SELECT id+1 FROM src.events", "postgres"
    )
    edge = dict(
        source_ref="c.src.events",
        source_column="id",
        target_ref="c.dw.summary",
        target_column="id",
        relation="value_direct",
        expression="id",
    )
    assert batch.edge_signature(edge) == batch.edge_signature(SimpleNamespace(**edge))


def test_model_cache_and_accepted_decision_are_reused_in_new_revision(memory, monkeypatch):
    from avelis.importer import assist

    calls = []

    def fake_assistance(**kwargs):
        calls.append(bool(kwargs["cached"]))
        statement = kwargs["rule_statements"][0]
        return assist.AssistanceResult(
            candidates=[
                {
                    "statement_id": statement.id,
                    "origin": "model",
                    "source_ref": "default_catalog.src.events",
                    "source_column": "id",
                    "target_ref": "default_catalog.dw.summary",
                    "target_column": "id",
                    "relation": "value_direct",
                    "evidence": {"lines": [1]},
                }
            ],
            payloads=[{"statement_id": statement.id, "text": "脱敏片段", "reused": bool(kwargs["cached"])}],
            cache={"fragment": {"result": "validated"}},
        )

    monkeypatch.setattr(assist, "run_assistance", fake_assistance)
    first = create(memory, "ANALYZE src.events;", model_assist={"failed_sql": True})
    batch.run_import(first.id)
    previous = memory.rows[CandidateEdge][0]
    old_job = memory.rows[ImportJob][0]
    stmt = memory.rows[ExtractedStatement][0]
    edge = batch._new_edge(
        memory.actor,
        old_job,
        stmt,
        {k: getattr(previous, k) for k in (*batch.EDGE_FIELDS, "expression")},
        {},
        origin="model_confirmed",
    )
    memory.session.add(edge)
    previous.status, previous.lineage_edge_id = "accepted", edge.id
    previous.decided_by, previous.decided_at, previous.reason = memory.actor.id, datetime.now(UTC), "确认来源"
    second = create(memory, "ANALYZE src.events; SELECT 1;", model_assist={"failed_sql": True})
    batch.run_import(second.id)
    assert calls == [False, True]
    inherited = memory.rows[CandidateEdge][-1]
    current_edge = memory.session.get(LineageEdge, inherited.lineage_edge_id)
    assert inherited.status == "accepted" and current_edge.origin == "model_confirmed"
    assert current_edge.attributes["carried_from"] == edge.id
    assert second.stats["invalidated_decisions"] == 0
    model_audit = [
        e for e in memory.rows[AuditEvent] if e.action == "import.revision" and e.detail.get("phase") == "model"
    ]
    assert model_audit[0].detail["inherited_decisions_total"] == 1


def test_model_virtual_statement_ids_are_replaced_in_candidates_and_payloads(memory, monkeypatch):
    from avelis.importer import assist

    def fake_assistance(**_kwargs):
        return assist.AssistanceResult(
            statements=[
                {
                    "id": "assist:0",
                    "seq": 2,
                    "line_start": 1,
                    "line_end": 1,
                    "method": "model_sql",
                    "raw_text": "source_code()",
                    "sql_text": "SELECT 1",
                    "status": "parsed",
                }
            ],
            candidates=[
                {
                    "statement_id": "assist:0",
                    "origin": "model_sql",
                    "source_ref": "default_catalog.src.events",
                    "source_column": "id",
                    "target_ref": "default_catalog.dw.summary",
                    "target_column": "id",
                    "relation": "value_direct",
                    "evidence": {"lines": [1]},
                }
            ],
            payloads=[{"statement_id": "assist:0", "text": "source_code()"}],
        )

    monkeypatch.setattr(assist, "run_assistance", fake_assistance)
    imported = create(memory, "SELECT 1;")
    batch.run_import(imported.id)
    candidate = memory.rows[CandidateEdge][0]
    assert candidate.statement_id != "assist:0"
    assert memory.session.get(ExtractedStatement, candidate.statement_id).method == "model_sql"
    assert memory.rows[ImportJob][0].model_payload[0]["statement_id"] == candidate.statement_id


def test_model_failure_in_one_file_does_not_prevent_following_files(memory, monkeypatch):
    from avelis.importer import assist

    calls = []

    def fake_assistance(**kwargs):
        calls.append(kwargs["content"])
        if len(calls) == 1:
            raise RuntimeError("辅助元数据错误")
        return assist.AssistanceResult()

    monkeypatch.setattr(assist, "run_assistance", fake_assistance)
    imported, _ = batch.create_batch(
        memory.session,
        memory.actor,
        datasource_id="ds",
        files=[ImportFile("a.sql", "SELECT 1;", "sql"), ImportFile("b.sql", "SELECT 2;", "sql")],
    )
    batch.run_import(imported.id)
    assert calls == ["SELECT 1;", "SELECT 2;"]
    stored = memory.session.get(ImportBatch, imported.id)
    assert stored.status == "done" and any("辅助元数据错误" in note for note in stored.notes)


def test_failed_background_job_reconciles_batch_and_attempt_fence_prevents_phase_two(memory, monkeypatch):
    imported = create(memory, "CREATE TABLE dw.summary AS SELECT id FROM src.events;")
    queued = memory.session.get(Job, imported.job_id)
    queued.status, queued.attempts = "running", 1
    prepare = batch._prepare

    def abandon_after_parse(*args, **kwargs):
        result = prepare(*args, **kwargs)
        queued.status, queued.error = "failed", "运行超过时限"
        return result

    monkeypatch.setattr(batch, "_prepare", abandon_after_parse)
    with pytest.raises(batch.Conflict, match="执行权"):
        batch.run_import(imported.id, job_id=queued.id, attempt=1)
    stored = batch.batch_or_404(memory.session, imported.id)
    assert stored.status == "failed"
    assert not memory.rows.get(LineageEdge) and not memory.rows.get(ExtractedStatement)


def test_old_attempt_cannot_mark_new_attempt_batch_failed(memory):
    imported = create(memory, "SELECT 1;")
    queued = memory.session.get(Job, imported.job_id)
    queued.status, queued.attempts = "running", 2
    with pytest.raises(batch.Conflict, match="执行权"):
        batch.run_import(imported.id, job_id=queued.id, attempt=1)
    assert memory.session.get(ImportBatch, imported.id).status == "queued"


def test_attempt_fence_protects_final_batch_status_after_deterministic_commit(memory, monkeypatch):
    imported = create(memory, "SELECT 1;")
    queued = memory.session.get(Job, imported.job_id)
    queued.status, queued.attempts = "running", 1

    def replace_attempt(*_args, **_kwargs):
        queued.attempts = 2

    monkeypatch.setattr(batch, "_assist", replace_attempt)
    with pytest.raises(batch.Conflict, match="执行权"):
        batch.run_import(imported.id, job_id=queued.id, attempt=1)
    stored = memory.session.get(ImportBatch, imported.id)
    assert stored.status == "running" and stored.progress["phase"] != "done"


def test_lost_attempt_cannot_append_model_failure_notes(memory, monkeypatch):
    from avelis.importer import assist

    imported = create(memory, "SELECT 1;")
    queued = memory.session.get(Job, imported.job_id)
    queued.status, queued.attempts = "running", 1

    def fail_after_attempt_changed(**_kwargs):
        queued.attempts = 2
        raise RuntimeError("旧执行的模型错误")

    monkeypatch.setattr(assist, "run_assistance", fail_after_attempt_changed)
    with pytest.raises(batch.Conflict, match="执行权"):
        batch.run_import(imported.id, job_id=queued.id, attempt=1)
    stored = memory.session.get(ImportBatch, imported.id)
    assert stored.status == "running" and not any("旧执行的模型错误" in note for note in stored.notes)


@pytest.mark.parametrize("decision", ["accept", "reject", "modify"])
def test_intermediate_decisions_are_rejected_without_changing_any_rows(memory, decision):
    imported = create(
        memory,
        "CREATE TEMPORARY TABLE dw.stage AS SELECT id FROM src.events;"
        "CREATE TABLE dw.summary AS SELECT id FROM dw.stage;",
    )
    batch.run_import(imported.id)
    candidate = next(c for c in memory.rows[CandidateEdge] if c.target_ref.endswith(".stage"))
    assert candidate.status == "intermediate"
    original = {
        kind: [
            {c.name: copy.deepcopy(getattr(row, c.name)) for c in kind.__table__.columns} for row in memory.rows[kind]
        ]
        for kind in (CandidateEdge, LineageEdge)
    }
    with pytest.raises(batch.Conflict, match="中间段只用于查证，请处理折叠后的边"):
        service.decide(memory.session, memory.actor, candidate, decision, "确认修订", {"source_column": "value"})
    for kind, rows in original.items():
        assert [{c.name: getattr(row, c.name) for c in kind.__table__.columns} for row in memory.rows[kind]] == rows


def test_accepting_model_sql_uses_model_confirmed_origin(memory, monkeypatch):
    monkeypatch.setattr(service, "resolve_qualified", lambda *_: None)
    imported = create(memory, "SELECT 1;")
    batch.run_import(imported.id)
    job = memory.rows[ImportJob][0]
    candidate = CandidateEdge(
        job_id=job.id,
        statement_id=memory.rows[ExtractedStatement][0].id,
        origin="model_sql",
        source_ref="default_catalog.src.events",
        source_column="id",
        target_ref="default_catalog.dw.summary",
        target_column="id",
        relation="value_direct",
        status="pending",
        evidence={"via": ["default_catalog.dw.stage"], "hops": ["trace-id"]},
    )
    memory.session.add(candidate)
    service.decide(memory.session, memory.actor, candidate, "accept", "确认模型还原", {})
    edge = memory.session.get(LineageEdge, candidate.lineage_edge_id)
    assert edge.origin == "model_confirmed"
    assert edge.attributes["via"] == ["default_catalog.dw.stage"] and edge.attributes["hops"] == ["trace-id"]


def test_unmatched_ignores_sources_written_earlier_in_batch_but_lists_new_targets():
    from avelis.importer.statements import StatementEdge, Write

    def parsed(*writes):
        return SimpleNamespace(writes=list(writes))

    insert = Write("c.dw.summary", ["k"], [StatementEdge("c.src.events", "k", "k", "value_direct", None)])
    update = Write("c.dw.report", ["k"], [StatementEdge("c.dw.summary", "k", "k", "value_direct", None)])
    statements = [SimpleNamespace(id="s1"), SimpleNamespace(id="s2")]
    prepared = [batch.PreparedFile(job=None, statements=statements, parsed=[parsed(insert), parsed(update)], notes=[])]
    found = {(e["ref"], e["role"]) for e in batch._unmatched(prepared, {"c.src.events": object()})}
    # summary 由本批 INSERT 写入，后面读取它不算缺口；两个未采集、也不是本批建出的目标仍列出
    assert found == {("c.dw.summary", "target"), ("c.dw.report", "target")}
