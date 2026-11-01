"""批量导入的 PostgreSQL 测试；只使用 tests/db 的独立元数据库，不访问源库或模型。"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import MetaData, Table, func, inspect, select, text
from sqlalchemy.schema import CreateSchema

from avelis.connectors.base import sha256_text
from avelis.db import get_engine, new_id, session_scope, utcnow
from avelis.importer import batch, service
from avelis.importer.archive import ImportFile
from avelis.models import (
    Asset,
    AssetColumn,
    AssetVersion,
    AuditEvent,
    CandidateEdge,
    CollectionRun,
    ColumnVersion,
    Datasource,
    ExtractedStatement,
    ImportBatch,
    ImportJob,
    ImportSource,
    Job,
    LineageEdge,
    User,
)

pytestmark = pytest.mark.db

SQL = "INSERT INTO dw.daily_summary (k, v) SELECT k, v FROM src.events;"
EXTRA_SQL = "INSERT INTO dw.daily_aux (k, v) SELECT k, v + 1 FROM src.events;"


@pytest.fixture
def legacy_schema(meta_db):
    """完整迁移到 0015；外层事务结束时连同随机 schema 一并回滚，不降级共享 schema。"""
    from alembic import command

    from avelis.devtools import alembic_config

    with get_engine().connect() as connection:
        transaction = connection.begin()
        try:
            schema = f"import_migration_{new_id()}"
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            cfg = alembic_config()
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "0015")
            yield connection, cfg, schema
        finally:
            transaction.rollback()


def _legacy_import(connection, *, filename="pipeline.sql", content=SQL, status="done", actor=None,
                   datasource=None, method="sql", created_at=None):
    """按 0015 的真实表结构写旧文件，避免用当前 ORM 的新增列。"""
    now = created_at or utcnow()
    if actor is None:
        actor = new_id()
        connection.execute(User.__table__.insert().values(
            id=actor, username=f"u{new_id()}", display_name="导入测试", password_hash="x", created_at=now))
    if datasource is None:
        datasource = new_id()
        connection.execute(Datasource.__table__.insert().values(
            id=datasource, name=f"import{new_id()}", kind="postgres", host="unused", port=1,
            created_by=actor, created_at=now))
    job_id, statement_id = new_id(), new_id()
    metadata = MetaData()
    jobs = Table("import_job", metadata, autoload_with=connection)
    statements = Table("extracted_statement", metadata, autoload_with=connection)
    values = dict(id=job_id, datasource_id=datasource, source_type="sql", filename=filename,
                  language="sql", dialect="postgres", default_db="dw", content_hash=sha256_text(content),
                  status=status, params={"schema": "dw"}, stats={"parsed": 1}, model_payload=[],
                  created_by=actor, created_at=now, updated_at=now)
    connection.execute(jobs.insert().values(**values))
    connection.execute(statements.insert().values(
        id=statement_id, job_id=job_id, seq=1, line_start=1, line_end=1, method=method,
        raw_text=content, sql_text=content if method == "sql" else None, placeholders=[], status="parsed"))
    connection.execute(ImportSource.__table__.insert().values(
        id=new_id(), job_id=job_id, content=content, content_hash=sha256_text(content)))
    return SimpleNamespace(job=job_id, statement=statement_id, actor=actor, datasource=datasource,
                           content=content, values=values, method=method)


def test_0016_backfills_one_batch_per_legacy_import_and_preserves_sources(legacy_schema):
    from alembic import command

    connection, cfg, schema = legacy_schema
    first = _legacy_import(connection)
    second = _legacy_import(connection, filename="transform.py", content='frame.to_sql("daily_summary")',
                            status="needs_params", actor=first.actor, datasource=first.datasource,
                            method="unrecognized")
    guard = connection.scalar(text("SELECT pg_get_functiondef('avelis_history_guard()'::regprocedure)"))
    command.upgrade(cfg, "0016")
    assert connection.scalar(text("SELECT pg_get_functiondef('avelis_history_guard()'::regprocedure)")) == guard
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0016"
    assert connection.scalar(text("SELECT count(*) FROM import_batch")) == 2
    for old in (first, second):
        job = connection.execute(text("SELECT * FROM import_job WHERE id = :id"), {"id": old.job}).mappings().one()
        imported = connection.execute(text("SELECT * FROM import_batch WHERE id = :id"),
                                      {"id": old.job}).mappings().one()
        statement = connection.execute(text("SELECT * FROM extracted_statement WHERE id = :id"),
                                       {"id": old.statement}).mappings().one()
        assert job["batch_id"] == old.job and job["source_key"] == old.values["filename"]
        assert job["revision"] == 1 and job["superseded_by"] is None
        assert imported["status"] == old.values["status"] and imported["created_by"] == old.actor
        assert imported["progress"] == {"files_total": 1, "files_done": 1, "statements_done": 1}
        assert imported["options"]["params"] == old.values["params"]
        assert imported["options"]["model_assist"] == {"failed_sql": False, "code": False}
        assert imported["stats"] == old.values["stats"]
        assert statement["text_hash"] == batch.normalized_text_hash(
            old.content, "postgres", sql=old.method != "unrecognized")
        assert statement["writes"] == [] and statement["raw_text"] == old.content
        assert connection.scalar(text("SELECT content FROM import_source WHERE job_id = :id"),
                                 {"id": old.job}) == old.content
    indexes = {item["name"]: item for item in inspect(connection).get_indexes("import_job", schema=schema)}
    assert indexes["ix_import_job_current_source"]["unique"]
    assert "superseded" in str(indexes["ix_import_job_current_source"]["dialect_options"]["postgresql_where"])
    constraints = inspect(connection).get_unique_constraints("import_job", schema=schema)
    assert "uq_import_job_datasource_id_content_hash" not in {item["name"] for item in constraints}


def test_0016_maps_legacy_model_status_and_keeps_pending_candidates_and_payload(legacy_schema):
    from alembic import command

    connection, cfg, _ = legacy_schema
    old = _legacy_import(connection, content='frame.to_sql("daily_summary")',
                         status="needs_review", method="unrecognized")
    payload = [{"text": "脱敏后的旧片段", "sent_to": "未发送（演示规则）"}]
    legacy = Table("import_job", MetaData(), autoload_with=connection)
    connection.execute(legacy.update().where(legacy.c.id == old.job).values(
        stats={"statements": {"model": 1, "unsupported": 2}, "parsed_edges": 4}, model_payload=payload))
    connection.execute(text("UPDATE extracted_statement SET status = 'model' WHERE id = :id"),
                       {"id": old.statement})
    candidate_id = new_id()
    connection.execute(CandidateEdge.__table__.insert().values(
        id=candidate_id, job_id=old.job, statement_id=old.statement, origin="model",
        source_ref="postgres.src.events", target_ref="postgres.dw.daily_summary", relation="table",
        evidence={"lines": [1]}, status="pending", modified={}, created_at=utcnow()))
    command.upgrade(cfg, "0016")
    for table in ("import_job", "import_batch"):
        row = connection.execute(text(f"SELECT status, stats FROM {table} WHERE id = :id"),
                                 {"id": old.job}).mappings().one()
        assert row["status"] == "needs_review"
        assert row["stats"] == {"statements": {"unsupported": 3}, "parsed_edges": 4}
    statement = connection.execute(text("SELECT status, raw_text FROM extracted_statement WHERE id = :id"),
                                   {"id": old.statement}).mappings().one()
    assert statement == {"status": "unsupported", "raw_text": old.content}
    assert connection.scalar(text("SELECT model_payload FROM import_job WHERE id = :id"), {"id": old.job}) == payload
    assert connection.scalar(text("SELECT status FROM candidate_edge WHERE id = :id"),
                             {"id": candidate_id}) == "pending"


def test_0016_backfills_duplicate_legacy_source_keys_without_superseding_imports_or_edges(legacy_schema):
    from alembic import command

    connection, cfg, _ = legacy_schema
    now = utcnow()
    filename = "粘贴的 SQL.sql"
    first = _legacy_import(connection, filename=filename, created_at=now - timedelta(days=1))
    second = _legacy_import(connection, filename=filename, content=EXTRA_SQL, status="needs_params",
                            actor=first.actor, datasource=first.datasource, created_at=now)
    third = _legacy_import(connection, filename=filename, content="SELECT 1;", status="needs_review",
                           actor=first.actor, datasource=first.datasource, created_at=now)
    other_source = _legacy_import(connection, filename=filename, actor=first.actor)
    old_imports = (first, second, third, other_source)
    for old in old_imports:
        connection.execute(LineageEdge.__table__.insert().values(
            id=new_id(), origin="import", import_job_id=old.job,
            source_ref="postgres.src.events", target_ref="postgres.dw.daily_summary",
            relation="table", status="active", created_at=now))
    before_edges = [dict(row) for row in connection.execute(text("SELECT * FROM lineage_edge ORDER BY id")).mappings()]

    command.upgrade(cfg, "0016")

    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0016"
    assert connection.scalar(text("SELECT count(*) FROM import_batch")) == len(old_imports)
    latest_id = max(second.job, third.job)
    for old in old_imports:
        row = connection.execute(text("SELECT * FROM import_job WHERE id = :id"), {"id": old.job}).mappings().one()
        expected_key = filename if old.job in (latest_id, other_source.job) else f"{filename}#{old.job[:8]}"
        assert row["source_key"] == expected_key
        assert row["batch_id"] == old.job and row["revision"] == 1 and row["superseded_by"] is None
        assert row["status"] == old.values["status"]
        assert connection.scalar(text("SELECT status FROM import_batch WHERE id = :id"),
                                 {"id": old.job}) == old.values["status"]
    after_edges = [dict(row) for row in connection.execute(text("SELECT * FROM lineage_edge ORDER BY id")).mappings()]
    assert after_edges == before_edges


@pytest.fixture
def import_world(meta_db, factory, monkeypatch):
    """真实写元数据和列结构；模型阶段与所有业务库连接入口均禁止调用。"""
    from avelis import engines

    actor = factory.user(("steward",))

    def forbidden(*args, **kwargs):
        pytest.fail("导入数据库测试不得请求业务库或真实模型")

    monkeypatch.setattr(engines, "query_connector", forbidden)
    monkeypatch.setattr(batch.modelcfg, "resolve", forbidden)
    monkeypatch.setattr(batch, "_assist", lambda *args, **kwargs: None)
    with session_scope() as session:
        source = Datasource(name=f"import{new_id()}", kind="postgres", host="unused", port=1,
                            options={}, created_by=actor)
        session.add(source)
        session.flush()
        run = CollectionRun(datasource_id=source.id, trigger="manual", status="succeeded")
        session.add(run)
        session.flush()
        assets = {}
        for db, name in (("src", "events"), ("dw", "daily_summary"), ("dw", "daily_aux")):
            asset = Asset(datasource_id=source.id, kind="table", catalog_name="postgres", db_name=db, name=name)
            session.add(asset)
            session.flush()
            version = AssetVersion(asset_id=asset.id, version_no=1, observed_run_id=run.id,
                                   last_seen_run_id=run.id, kind="table", catalog_name="postgres", db_name=db,
                                   name=name, content_hash=sha256_text(f"{db}.{name}"))
            session.add(version)
            session.flush()
            for ordinal, name in enumerate(("k", "v"), 1):
                column = AssetColumn(asset_id=asset.id, name=name)
                session.add(column)
                session.flush()
                session.add(ColumnVersion(asset_version_id=version.id, column_id=column.id,
                                          ordinal=ordinal, name=name, data_type="BIGINT"))
            asset.current_version_id = version.id
            assets[f"{asset.db_name}.{asset.name}"] = (asset.id, version.id)
        source_id = source.id
    return SimpleNamespace(actor=actor, datasource=source_id, assets=assets)


def _create_batch(world, content):
    with session_scope() as session:
        imported, created = batch.create_batch(
            session, session.get(User, world.actor), datasource_id=world.datasource,
            files=[ImportFile("pipeline.sql", content, "sql")], default_db="dw")
        assert created
        file = session.scalar(select(ImportJob).where(ImportJob.batch_id == imported.id))
        queued = session.get(Job, imported.job_id)
        assert queued.kind == "import" and queued.payload == {"batch_id": imported.id}
        batch_id, file_id = imported.id, file.id
    return batch_id, file_id


def test_phase_two_failure_rolls_back_statements_edges_and_previous_revision(import_world, monkeypatch):
    old_batch, old_file = _create_batch(import_world, SQL)
    assert batch.run_import(old_batch)["status"] == "done"
    with session_scope() as session:
        old_edges = {edge.id: edge.row_hash for edge in session.scalars(select(LineageEdge).where(
            LineageEdge.import_job_id == old_file))}
        assert len(old_edges) == 2 and all(old_edges.values())
    new_batch, new_file = _create_batch(import_world, SQL + "\n" + EXTRA_SQL)
    original, writes = batch._persist_edge, []

    def fail_partway(session, *args, **kwargs):
        writes.append(kwargs.get("edge_id"))
        if len(writes) == 2:
            raise RuntimeError("模拟阶段二写入异常")
        result = original(session, *args, **kwargs)
        session.flush()
        assert session.scalar(select(func.count()).select_from(ExtractedStatement).where(
            ExtractedStatement.job_id == new_file)) == 2
        return result

    monkeypatch.setattr(batch, "_persist_edge", fail_partway)
    with pytest.raises(RuntimeError, match="模拟阶段二写入异常"):
        batch.run_import(new_batch)
    assert len(writes) == 2
    with session_scope() as session:
        previous = session.get(ImportJob, old_file)
        assert previous.status == "done" and previous.superseded_by is None
        for edge_id, digest in old_edges.items():
            edge = session.get(LineageEdge, edge_id)
            assert edge.status == "active" and edge.superseded_at is None and edge.row_hash == digest
        assert session.get(ImportBatch, new_batch).status == "failed"
        assert session.get(ImportJob, new_file).stats["staged"] is True
        for model, column in ((ExtractedStatement, ExtractedStatement.job_id),
                              (CandidateEdge, CandidateEdge.job_id), (LineageEdge, LineageEdge.import_job_id)):
            assert session.scalar(select(func.count()).select_from(model).where(column == new_file)) == 0
        source = session.scalar(select(ImportSource.content).where(ImportSource.job_id == new_file))
        assert source == SQL + "\n" + EXTRA_SQL


def test_reimport_supersedes_old_edges_and_carries_rejection_without_changing_observed_definition(import_world):
    old_batch, old_file = _create_batch(import_world, SQL)
    assert batch.run_import(old_batch)["status"] == "done"
    with session_scope() as session:
        rejected = session.scalar(select(CandidateEdge).where(
            CandidateEdge.job_id == old_file, CandidateEdge.target_column == "k"))
        assert rejected is not None and rejected.status == "written"
        rejected_id = rejected.id
        service.decide(session, session.get(User, import_world.actor), rejected, "reject", "该对应关系不成立", {})
        source_id, _ = import_world.assets["src.events"]
        target_id, target_version = import_world.assets["dw.daily_summary"]
        observed = LineageEdge(origin="parsed", import_job_id=old_file, source_asset_id=source_id,
                               source_ref="postgres.src.events", source_column="v", target_asset_id=target_id,
                               target_asset_version_id=target_version, target_ref="postgres.dw.daily_summary",
                               target_column="v", relation="value_direct", status="active")
        session.add(observed)
        session.flush()
        observed_id = observed.id
    with session_scope() as session:
        observed_hash = session.get(LineageEdge, observed_id).row_hash
        old_edges = {edge.id: edge.row_hash for edge in session.scalars(select(LineageEdge).where(
            LineageEdge.import_job_id == old_file, LineageEdge.origin == "import"))}
    new_batch, new_file = _create_batch(import_world, SQL + "\n" + EXTRA_SQL)
    assert batch.run_import(new_batch)["status"] == "done"
    with session_scope() as session:
        previous, current = session.get(ImportJob, old_file), session.get(ImportJob, new_file)
        assert previous.status == "superseded" and previous.superseded_by == current.id
        assert current.source_key == "pipeline.sql" and current.revision == 2
        assert current.stats["previous_id"] == old_file
        assert current.stats["replaced_edges"] == 1 and current.stats["inherited_decisions"] == 1
        for edge_id, digest in old_edges.items():
            edge = session.get(LineageEdge, edge_id)
            assert edge.status == "superseded" and edge.superseded_at is not None and edge.row_hash == digest
        inherited = session.scalar(select(CandidateEdge).where(
            CandidateEdge.job_id == new_file, CandidateEdge.target_ref == "postgres.dw.daily_summary",
            CandidateEdge.target_column == "k"))
        assert inherited.status == "rejected" and inherited.lineage_edge_id is not None
        rejected_edge = session.get(LineageEdge, inherited.lineage_edge_id)
        assert rejected_edge.status == "superseded" and rejected_edge.superseded_at is not None
        assert inherited.reason == "该对应关系不成立" and inherited.decided_by == import_world.actor
        assert inherited.evidence["carried_decision_from"] == rejected_id
        active = list(session.scalars(select(LineageEdge).where(
            LineageEdge.import_job_id == new_file, LineageEdge.status == "active")))
        assert len(active) == 3 and all(edge.chain_seq is not None and edge.row_hash for edge in active)
        observed = session.get(LineageEdge, observed_id)
        assert observed.status == "active" and observed.row_hash == observed_hash and observed.superseded_at is None
        assert session.get(Asset, target_id).current_version_id == target_version
        revision = session.scalar(select(AuditEvent).where(
            AuditEvent.action == "import.revision", AuditEvent.object_id == new_file))
        assert revision.detail["previous_id"] == old_file and revision.detail["inherited_decisions"] == 1
        assert session.scalar(select(ImportSource.content).where(ImportSource.job_id == old_file)) == SQL


def test_reimport_preserves_intermediate_edges_and_hash_chain(import_world):
    from sqlalchemy.orm import Session

    from avelis.history import integrity

    content = ("CREATE TEMP TABLE staged AS SELECT k, v FROM src.events;\n"
               "INSERT INTO dw.daily_summary (k, v) SELECT k, v FROM staged;")
    old_batch, old_file = _create_batch(import_world, content)
    assert batch.run_import(old_batch)["status"] == "done"
    with session_scope() as session:
        edges = list(session.scalars(select(LineageEdge).where(LineageEdge.import_job_id == old_file)))
        old_edges = {edge.id: (edge.status, edge.row_hash) for edge in edges}
        assert {edge.status for edge in edges} == {"intermediate", "active"}
        assert all(edge.row_hash and edge.chain_seq is not None for edge in edges)
        intermediate_ids = {edge.id for edge in edges if edge.status == "intermediate"}
        assert intermediate_ids
        intermediate_rows = {row["id"]: dict(row) for row in session.execute(
            select(LineageEdge.__table__).where(LineageEdge.id.in_(intermediate_ids))).mappings()}
        for edge in edges:
            if edge.status == "active":
                assert edge.attributes["via"] and set(edge.attributes["hops"]) <= intermediate_ids
    new_batch, new_file = _create_batch(import_world, content + "\nSELECT 1;")
    assert batch.run_import(new_batch)["status"] == "done"
    with session_scope() as session:
        assert session.get(ImportJob, old_file).superseded_by == new_file
        for edge_id, (status, digest) in old_edges.items():
            edge = session.get(LineageEdge, edge_id)
            if status == "intermediate":
                row = session.execute(select(LineageEdge.__table__).where(LineageEdge.id == edge_id)).mappings().one()
                assert dict(row) == intermediate_rows[edge_id]
                assert edge.status == "intermediate" and edge.superseded_at is None
            else:
                assert edge.status == "superseded" and edge.superseded_at is not None
            assert edge.row_hash == digest
        assert session.get(ImportJob, new_file).stats["replaced_edges"] == sum(
            status == "active" for status, _ in old_edges.values())
        edges = list(session.scalars(select(LineageEdge).where(LineageEdge.import_job_id == new_file)))
        assert {edge.status for edge in edges} == {"intermediate", "active"}
        new_intermediate = {edge.id for edge in edges if edge.status == "intermediate"}
        for edge in edges:
            if edge.status == "active":
                assert set(edge.attributes["hops"]) <= new_intermediate
    with get_engine().connect() as connection:
        connection = connection.execution_options(isolation_level="REPEATABLE READ")
        with Session(bind=connection) as session:
            verified = integrity.verify(session)
            assert verified["ok"], verified
