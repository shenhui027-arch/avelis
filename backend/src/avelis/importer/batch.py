"""批量导入：解析在内存中进行，确定性结果与旧版本替换在同一事务提交。

谁把批次从 queued/running 改成 failed，谁在同一事务写失败事件；failure_guard 只兜底。
"""

from __future__ import annotations

import copy
import logging
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

import sqlglot
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from avelis import audit, events, jobs, modelcfg
from avelis.collect.service import build_schema
from avelis.config import get_settings
from avelis.connectors.base import sha256_text
from avelis.db import new_id, session_scope, utcnow
from avelis.engines import dialect_of, engine_of
from avelis.governance.errors import Conflict, Invalid, NotFound
from avelis.i18n import Msg
from avelis.importer import service
from avelis.importer.archive import FileDependencies, ImportFile, order_files
from avelis.importer.extract import extract_python, extract_shell, substitute
from avelis.importer.fold import FoldStatement, fold_writes
from avelis.importer.split import split_sql
from avelis.importer.statements import parse_statement
from avelis.lineage.sql import SqlDialect, canonical_hash_of
from avelis.models import (
    Asset,
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

MAX_STATEMENT_BYTES = 1024 * 1024
EDGE_FIELDS = ("source_ref", "source_column", "target_ref", "target_column", "relation")
log = logging.getLogger(__name__)


class ImportAbandoned(Conflict):
    """已经失去执行权的尝试不得覆盖终态，也不重复已留底的失败事件。"""

    failure_event_recorded = True


def _file_order(batch: ImportBatch) -> dict[str, int]:
    order = {}
    for index, job_id in enumerate((batch.options or {}).get("file_order", [])):
        order.setdefault(job_id, index)
    return order


def create_batch(
    session: Session,
    actor: User,
    *,
    datasource_id: str,
    files: list[ImportFile],
    default_db: str | None = None,
    params: dict[str, str] | None = None,
    model_assist: dict[str, bool] | None = None,
    sql_functions: list[str] | None = None,
    notes: list[str] | None = None,
    force: bool = False,
) -> tuple[ImportBatch, bool]:
    service.require_import_access(session, actor)
    ds = session.get(Datasource, datasource_id)
    if ds is None:
        raise NotFound("数据源不存在")
    if not files:
        raise Invalid("没有可导入的文件")
    settings = get_settings()
    if any(len(f.content.encode("utf-8")) > settings.import_max_bytes for f in files):
        raise Invalid("文件超过导入大小上限")
    if sum(len(f.content.encode("utf-8")) for f in files) > settings.import_max_batch_bytes:
        raise Invalid("批次超过导入总大小上限")
    keys = [f.source_key or f.filename for f in files]
    if len(set(keys)) != len(keys):
        raise Invalid("同一批次的来源键不能重复")
    current = list(
        session.scalars(
            select(ImportJob).where(ImportJob.datasource_id == datasource_id, ImportJob.status != "superseded")
        )
    )
    by_key = {j.source_key: j for j in current}
    by_hash = {j.content_hash: j for j in current}
    duplicate_ids, pending, file_order = [], [], {}
    for upload_order, file in enumerate(files):
        if not file.content.strip():
            raise Invalid(Msg("文件 {name} 的导入内容为空", name=file.filename))
        digest = sha256_text(file.content)
        duplicate = by_hash.get(digest) if not force else None
        if duplicate:
            duplicate_ids.append(duplicate.id)
            file_order[upload_order] = duplicate.id
            continue
        pending.append((file, digest, by_key.get(file.source_key or file.filename), upload_order))
    if not pending and duplicate_ids:
        existing = session.get(ImportBatch, session.get(ImportJob, duplicate_ids[0]).batch_id)
        if existing is not None and all(session.get(ImportJob, i).batch_id == existing.id for i in duplicate_ids):
            audit.record(session, actor, "import.duplicate", "import_batch", existing.id)
            return existing, False
    batch = ImportBatch(
        id=new_id(),
        datasource_id=datasource_id,
        default_db=default_db,
        status="queued",
        progress={
            "files_total": len(pending) + len(set(duplicate_ids)),
            "files_done": 0,
            "statements_done": 0,
            "current_file": None,
            "phase": "queued",
        },
        stats={"params_rewrite": force},
        notes=list(notes or []),
        options={
            "params": dict(params or {}),
            "model_assist": {
                "failed_sql": bool((model_assist or {}).get("failed_sql")),
                "code": bool((model_assist or {}).get("code")),
            },
            "sql_functions": list(dict.fromkeys(sql_functions or [])),
            "duplicate_file_ids": list(dict.fromkeys(duplicate_ids)),
        },
        created_by=actor.id,
    )
    session.add(batch)
    session.flush()
    for file, digest, previous, upload_order in pending:
        # 预留文件尚未生效；只有阶段二才切换当前版本，避免排队时影响已确认血缘。
        job = ImportJob(
            id=new_id(),
            batch_id=batch.id,
            datasource_id=datasource_id,
            source_key=file.source_key or file.filename,
            revision=(previous.revision + 1) if previous else 1,
            filename=file.filename,
            language=file.language,
            source_type=file.language,
            dialect=dialect_of(ds).name,
            default_db=default_db,
            content_hash=digest,
            status="superseded",
            params=dict(params or {}),
            created_by=actor.id,
            stats={"staged": True, "previous_id": previous.id if previous else None, "upload_order": upload_order},
        )
        session.add(job)
        session.flush()
        session.add(ImportSource(job_id=job.id, content=file.content, content_hash=digest))
        file_order[upload_order] = job.id
    batch.options = {**batch.options, "file_order": [file_order[i] for i in sorted(file_order)]}
    queued = jobs._enqueue(session, "import", {"batch_id": batch.id}, actor.id)
    batch.job_id = queued
    audit.record(session, actor, "import.create", "import_batch", batch.id, files=len(pending))
    session.flush()
    return batch, True


def set_params(session: Session, actor: User, batch: ImportBatch, params: dict[str, str]) -> ImportBatch:
    service.require_import_access(session, actor)
    if batch.status in ("queued", "running"):
        raise Conflict("批次正在执行，不能补充参数")
    files = []
    jobs = [
        j
        for j in session.scalars(
            select(ImportJob).where(ImportJob.batch_id == batch.id).order_by(ImportJob.created_at, ImportJob.id)
        )
        if not (j.stats or {}).get("duplicate_of")
    ]
    duplicate_ids = (batch.options or {}).get("duplicate_file_ids", [])
    if duplicate_ids:
        jobs.extend(session.scalars(select(ImportJob).where(ImportJob.id.in_(duplicate_ids))))
    jobs = list({j.id: j for j in jobs}.values())
    order = _file_order(batch)
    jobs.sort(key=lambda j: order.get(j.id, (j.stats or {}).get("upload_order", 0)))
    for job in jobs:
        content = session.scalar(select(ImportSource.content).where(ImportSource.job_id == job.id))
        files.append(ImportFile(job.filename, content, job.language, job.source_key, len(content.encode("utf-8"))))
    options = batch.options or {}
    result, _ = create_batch(
        session,
        actor,
        datasource_id=batch.datasource_id,
        files=files,
        default_db=batch.default_db,
        params={**options.get("params", {}), **params},
        model_assist=options.get("model_assist"),
        sql_functions=options.get("sql_functions"),
        notes=["补充参数后整批重新解析"],
        force=True,
    )
    audit.record(
        session, actor, "import.params", "import_batch", result.id, previous_batch_id=batch.id, params=sorted(params)
    )
    return result


rewrite_params = set_params


def normalized_text_hash(text: str, dialect_name: str, *, sql: bool = True) -> str:
    """SQL 使用语法树规范化；不能解析的片段保留原文，绝不改写 SQL。"""
    if sql:
        try:
            return canonical_hash_of(text, SqlDialect(dialect_name, ""))[1]
        except sqlglot.errors.SqlglotError:
            pass
    return sha256_text(text.strip())


def edge_signature(edge: Any) -> tuple:
    return tuple(edge.get(k) if isinstance(edge, dict) else getattr(edge, k) for k in EDGE_FIELDS)


def decision_index(statements: list[ExtractedStatement], candidates: list[CandidateEdge]) -> dict[tuple, CandidateEdge]:
    hashes = {s.id: s.text_hash for s in statements}
    return {
        (hashes[c.statement_id], edge_signature(c)): c
        for c in candidates
        if c.statement_id in hashes and c.status in ("accepted", "modified", "rejected")
    }


def _segments(job: ImportJob, content: str, dialect: Any, separators: tuple, functions: list[str]):
    if job.language == "python":
        return extract_python(content, dialect_name=dialect.name, separators=separators, sql_functions=functions)
    if job.language == "shell":
        return extract_shell(content, dialect_name=dialect.name, separators=separators, sql_functions=functions)
    notes: list[str] = []
    result = split_sql(content, dialect.name, separators, notes=notes)
    return result, notes


def _dependencies(
    segments: list, dialect: Any, default_db: str | None, params: dict[str, str] | None = None
) -> FileDependencies:
    reads, writes = set(), set()
    for segment in segments:
        if segment.method == "unrecognized" or len(segment.text.encode("utf-8")) > MAX_STATEMENT_BYTES:
            continue
        sql, missing = substitute(segment.text, params or {})
        if missing:
            continue
        try:
            tree = sqlglot.parse_one(sql, read=dialect.name)
        except sqlglot.errors.SqlglotError:
            continue
        if tree is None:
            continue
        tree = normalize_identifiers(dialect.prepare(tree), dialect=dialect.name)
        if isinstance(tree, exp.Use):
            default_db = tree.this.name if tree.this else default_db

        def qualify(table, db=default_db):
            return service._qualify(".".join(p.name for p in table.parts), db, dialect.default_catalog)

        renamed = False
        for alter in tree.expressions if isinstance(tree, exp.RenameTable) else [tree]:
            if not (isinstance(alter, exp.Alter) and isinstance(alter.this, exp.Table)):
                continue
            actions = alter.args.get("actions") or []
            if len(actions) == 1 and isinstance(actions[0], (exp.AlterRename, exp.SwapTable)):
                renamed = True
                action = actions[0]
                old = qualify(alter.this)
                new = qualify(action.this, alter.this.db or default_db)
                if old and new:
                    reads.add(old)
                    writes.add(new)
                    if isinstance(action, exp.SwapTable):
                        reads.add(new)
                        writes.add(old)
        if renamed:
            continue
        targets = []
        for node in tree.walk():
            if isinstance(node, (exp.Insert, exp.Create, exp.Update, exp.Merge)):
                target = node.this.this if isinstance(node.this, exp.Schema) else node.this
                if isinstance(target, exp.Table):
                    targets.append(target)
        target_refs = {q for t in targets if (q := qualify(t))}
        writes.update(target_refs)
        target_ids = {id(t) for t in targets}
        cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
        reads.update(
            q
            for t in tree.find_all(exp.Table)
            if id(t) not in target_ids and (t.db or t.name not in cte_names) and (q := qualify(t))
        )
        if isinstance(tree, (exp.Update, exp.Merge)):
            reads.update(target_refs)
    return FileDependencies(reads=reads, writes=writes)


@dataclass
class PreparedFile:
    job: ImportJob
    statements: list[ExtractedStatement]
    parsed: list[Any]
    notes: list[str]
    reused: bool = False
    statement_contexts: list[dict[str, Any]] = field(default_factory=list)


def _progress(batch_id: str, **values) -> None:
    # 与解析或写入事务分离，轮询请求可以及时看见。
    with session_scope() as session:
        batch = session.get(ImportBatch, batch_id)
        reconcile_batch(session, batch)
        if batch.status == "failed":
            return
        batch.status = "running"
        batch.progress = {**(batch.progress or {}), **values}
        batch.updated_at = utcnow()


def _prepare(
    batch: ImportBatch,
    files: list[ImportJob],
    contents: dict[str, str],
    schema: dict,
    dialect: Any,
    separators: tuple,
    model_schema: dict | None = None,
    reused_statements: dict[str, dict[int, ExtractedStatement]] | None = None,
) -> tuple[list[PreparedFile], list[str]]:
    options, extracted, notes = batch.options or {}, {}, []
    for job in files:
        extracted[job.id] = _segments(job, contents[job.id], dialect, separators, options.get("sql_functions", []))
    dependencies = [_dependencies(extracted[j.id][0], dialect, j.default_db, j.params) for j in files]
    ordered, order_notes = order_files(files, dependencies)
    notes.extend(order_notes)
    prepared, count, last_progress = [], 0, time.monotonic()
    for file_index, job in enumerate(ordered):
        default_db, statements, parsed_results = job.default_db, [], []
        statement_contexts = []
        segments, file_notes = extracted[job.id]
        reusable = (reused_statements or {}).get(job.id, {})
        for seq, segment in enumerate(segments, 1):
            statement_db = default_db
            original = reusable.get(seq)
            stmt = ExtractedStatement(
                id=original.id if original else new_id(),
                job_id=job.id,
                seq=seq,
                line_start=segment.line_start,
                line_end=segment.line_end,
                method=segment.method,
                raw_text=segment.text,
                placeholders=segment.placeholders,
                status="unsupported",
                writes=[],
                text_hash=normalized_text_hash(segment.text, dialect.name, sql=segment.method != "unrecognized"),
            )
            parsed = None
            if len(segment.text.encode("utf-8")) > MAX_STATEMENT_BYTES:
                stmt.status, stmt.reason = "failed", "语句过大（超过 1 MB）"
            elif segment.method == "unrecognized":
                stmt.reason = "确定性解析无法覆盖（非 SQL 的数据操作），等待模型候选"
            else:
                sql, missing = substitute(segment.text, job.params or {})
                if missing:
                    stmt.status, stmt.reason = "needs_params", f"动态 SQL，需要补充参数：{', '.join(missing)}"
                else:
                    parsed = parse_statement(
                        sql, dialect=dialect, schema=schema, default_db=default_db, line_start=stmt.line_start
                    )
                    stmt.sql_text, stmt.status, stmt.reason = sql, parsed.status, parsed.reason
                    stmt.statement_type, stmt.target_ref = parsed.statement_type, parsed.target_ref
                    stmt.writes = [asdict(w) for w in parsed.writes]
                    default_db = parsed.use_db or default_db
                    for ref in [*getattr(parsed, "dropped_refs", []), *getattr(parsed, "removed_refs", [])]:
                        catalog, db, name = ref.split(".")
                        schema.get(catalog, {}).get(db, {}).pop(name, None)
                    for write in parsed.writes:
                        if write.target_columns:
                            catalog, db, name = write.target_ref.split(".")
                            tables = schema.setdefault(catalog, {}).setdefault(db, {})
                            if write.created or name not in tables:
                                tables[name] = {c: "VARCHAR" for c in write.target_columns}
                            else:
                                tables[name].update({c: tables[name].get(c, "VARCHAR") for c in write.target_columns})
                            if model_schema is not None:
                                model_schema.setdefault(catalog, {}).setdefault(db, {})[name] = dict(tables[name])
            statement_contexts.append(
                {
                    "seq": seq,
                    "line_start": stmt.line_start,
                    "line_end": stmt.line_end,
                    "default_db": statement_db,
                    "default_db_after": default_db,
                }
            )
            statements.append(stmt)
            parsed_results.append(parsed)
            count += 1
            if count % 200 == 0 or time.monotonic() - last_progress >= 1:
                _progress(
                    batch.id,
                    phase="parse",
                    files_total=len(files),
                    files_done=file_index,
                    statements_done=count,
                    current_file=job.filename,
                )
                last_progress = time.monotonic()
        prepared.append(
            PreparedFile(
                job,
                statements,
                parsed_results,
                file_notes,
                reused=job.id in (reused_statements or {}),
                statement_contexts=statement_contexts,
            )
        )
    _progress(
        batch.id, phase="write", files_total=len(files), files_done=len(files), statements_done=count, current_file=None
    )
    return prepared, notes


def _asset_map(session: Session, datasource_id: str) -> dict[str, Asset]:
    return {
        f"{a.catalog_name}.{a.db_name}.{a.name}": a
        for a in session.scalars(select(Asset).where(Asset.datasource_id == datasource_id, Asset.status == "active"))
    }


def _new_edge(
    actor: User,
    job: ImportJob,
    stmt: ExtractedStatement | None,
    values: dict,
    assets: dict,
    *,
    status: str = "active",
    edge_id: str | None = None,
    origin: str = "import",
    attributes: dict | None = None,
    reason: str | None = None,
) -> LineageEdge:
    source, target = assets.get(values["source_ref"]), assets.get(values["target_ref"])
    return LineageEdge(
        id=edge_id or new_id(),
        origin=origin,
        import_job_id=job.id,
        source_asset_id=source.id if source else None,
        target_asset_id=target.id if target else None,
        target_asset_version_id=target.current_version_id if target else None,
        **{k: values.get(k) for k in (*EDGE_FIELDS, "expression")},
        status=status,
        created_by=actor.id,
        reason=reason,
        attributes={
            "statement_id": stmt.id if stmt else None,
            "job": job.filename,
            "lines": [stmt.line_start, stmt.line_end] if stmt else None,
            "design_time": True,
            **(attributes or {}),
        },
    )


def _persist_edge(
    session: Session,
    actor: User,
    job: ImportJob,
    stmt: ExtractedStatement | None,
    values: dict,
    assets: dict,
    *,
    candidate_origin: str,
    status: str = "active",
    edge_id: str | None = None,
    attributes: dict | None = None,
    previous: CandidateEdge | None = None,
) -> CandidateEdge:
    evidence = {
        "lines": [stmt.line_start, stmt.line_end] if stmt else None,
        **(attributes or {}),
        **values.get("evidence", {}),
    }
    if status == "intermediate":
        previous = None
    candidate = CandidateEdge(
        id=new_id(),
        job_id=job.id,
        statement_id=stmt.id if stmt else None,
        origin=candidate_origin,
        **{k: values.get(k) for k in (*EDGE_FIELDS, "expression")},
        confidence=values.get("confidence"),
        evidence=evidence,
        status="intermediate" if status == "intermediate" else "written",
    )
    if previous is not None:
        candidate.status, candidate.reason = previous.status, previous.reason
        candidate.decided_by, candidate.decided_at = previous.decided_by, previous.decided_at
        candidate.modified = dict(previous.modified or {})
        candidate.evidence = {**evidence, "carried_decision_from": previous.id}
        if previous.status == "rejected" and edge_id is None:
            session.add(candidate)
            return candidate
    actual = {**values, **(previous.modified or {})} if previous is not None else values
    origin = (
        ("model_confirmed" if candidate_origin in ("model", "model_sql") else "import_manual")
        if previous is not None and previous.status != "rejected"
        else "import"
    )
    if previous is not None and previous.status == "rejected":
        status = "superseded"
    attributes = {**(attributes or {}), **({"carried_from": previous.lineage_edge_id} if previous else {})}
    edge = _new_edge(
        actor,
        job,
        stmt,
        actual,
        assets,
        status=status,
        edge_id=edge_id,
        origin=origin,
        attributes=attributes,
        reason=previous.reason if previous else None,
    )
    if status == "superseded":
        edge.superseded_at = utcnow()
    candidate.lineage_edge_id = edge.id
    # LineageEdge 属于哈希链：保留 ORM 插入与数据库封链触发器。
    session.add(edge)
    session.add(candidate)
    return candidate


def _previous_data(session: Session, previous: ImportJob | None):
    if previous is None:
        return {}, {}, []
    statements = list(session.scalars(select(ExtractedStatement).where(ExtractedStatement.job_id == previous.id)))
    candidates = list(session.scalars(select(CandidateEdge).where(CandidateEdge.job_id == previous.id)))
    return decision_index(statements, candidates), (previous.stats or {}).get("assist_cache", {}), candidates


def _persist_prepared(
    session: Session, actor: User, batch: ImportBatch, prepared: list[PreparedFile], assets: dict, notes: list[str]
) -> None:
    jobs_by_id, statements_by_id, decisions, matched = {}, {}, {}, set()
    totals = Counter()
    old_counts = {}
    duplicates = {}
    active_files = []
    for file in prepared:
        job = session.get(ImportJob, file.job.id)
        duplicate = (
            None
            if file.reused
            else session.scalar(
                select(ImportJob).where(
                    ImportJob.datasource_id == batch.datasource_id,
                    ImportJob.content_hash == job.content_hash,
                    ImportJob.status != "superseded",
                    ImportJob.id != job.id,
                )
            )
        )
        if duplicate is not None and not (batch.stats or {}).get("params_rewrite"):
            duplicates[job.id] = duplicate.id
            job.stats = {"duplicate_of": duplicate.id, "upload_order": (job.stats or {}).get("upload_order", 0)}
            audit.record(session, actor, "import.duplicate", "import_job", duplicate.id, staged_job_id=job.id)
            file.reused, file.job = True, duplicate
            existing = {
                s.seq: s
                for s in session.scalars(select(ExtractedStatement).where(ExtractedStatement.job_id == duplicate.id))
            }
            for stmt in file.statements:
                stmt.job_id = duplicate.id
                if stmt.seq in existing:
                    stmt.id = existing[stmt.seq].id
            job = duplicate
        if job.id in jobs_by_id:
            continue
        active_files.append(file)
        if file.reused:
            if job.superseded_by is not None:
                raise Conflict(Msg("重复文件 {name} 已被更新，请重新导入该批次", name=job.filename))
            index, _, old_candidates = _previous_data(session, job)
            decisions[job.id] = index
            old_counts[job.id] = set()
            jobs_by_id[job.id] = job
            for stmt in file.statements:
                stored = session.get(ExtractedStatement, stmt.id)
                if stored is None:
                    stored = stmt
                    session.add(stored)
                else:
                    for field in ("status", "reason", "sql_text", "statement_type", "target_ref", "writes"):
                        setattr(stored, field, getattr(stmt, field))
                statements_by_id[stmt.id] = stored
            session.flush()
            job.stats = {
                **(job.stats or {}),
                "statement_contexts": file.statement_contexts,
                "statements": dict(
                    Counter(
                        s.status
                        for s in session.scalars(select(ExtractedStatement).where(ExtractedStatement.job_id == job.id))
                    )
                ),
            }
            continue
        previous = session.scalar(
            select(ImportJob)
            .where(
                ImportJob.datasource_id == batch.datasource_id,
                ImportJob.source_key == job.source_key,
                ImportJob.status != "superseded",
                ImportJob.id != job.id,
            )
            .with_for_update()
        )
        index, cache, old_candidates = _previous_data(session, previous)
        decisions[job.id] = index
        old_counts[job.id] = {c.id for c in old_candidates if c.status in ("accepted", "modified", "rejected")}
        replaced = 0
        if previous:
            replaced = session.execute(
                update(LineageEdge)
                .where(
                    LineageEdge.import_job_id == previous.id,
                    LineageEdge.origin.in_(service.CONFIRMED_ORIGINS),
                    LineageEdge.status == "active",
                )
                .values(status="superseded", superseded_at=utcnow())
            ).rowcount
            previous.status, previous.superseded_by, previous.updated_at = "superseded", job.id, utcnow()
            job.revision = previous.revision + 1
            session.flush()
        job.status = "needs_params" if any(s.status == "needs_params" for s in file.statements) else "done"
        job.stats = {
            "previous_id": previous.id if previous else None,
            "replaced_edges": replaced,
            "upload_order": (job.stats or {}).get("upload_order", 0),
            "notes": file.notes,
            "statements": dict(Counter(s.status for s in file.statements)),
            "assist_cache": cache,
            "statement_contexts": file.statement_contexts,
        }
        jobs_by_id[job.id] = job
        for stmt in file.statements:
            statements_by_id[stmt.id] = stmt
        totals["replaced_edges"] += replaced
    prepared[:] = active_files
    if duplicates:
        options = batch.options or {}
        batch.options = {
            **options,
            "duplicate_file_ids": list(dict.fromkeys([*options.get("duplicate_file_ids", []), *duplicates.values()])),
            "file_order": [duplicates.get(i, i) for i in options.get("file_order", [])],
        }
    session.add_all([stmt for file in prepared if not file.reused for stmt in file.statements])
    session.flush()
    folds = []
    for file in prepared:
        for stmt, parsed in zip(file.statements, file.parsed):
            if parsed is not None:
                folds.append(
                    FoldStatement(
                        job_id=file.job.id,
                        statement_id=stmt.id,
                        writes=parsed.writes,
                        dropped_refs=getattr(parsed, "dropped_refs", []),
                        reset_refs=getattr(parsed, "reset_refs", []),
                        removed_refs=getattr(parsed, "removed_refs", []),
                    )
                )
    folded = fold_writes(folds)
    reused_ids = {f.job.id for f in prepared if f.reused}
    existing_edges = (
        {e.id: e for e in session.scalars(select(LineageEdge).where(LineageEdge.import_job_id.in_(reused_ids)))}
        if reused_ids
        else {}
    )
    reuse, remap, previous_by_id = set(), {}, {}
    for item in folded.edges:
        stmt = statements_by_id[item.statement_id]
        old = decisions[item.job_id].get((stmt.text_hash, edge_signature(item))) if item.status == "active" else None
        previous_by_id[item.id] = old
        existing = existing_edges.get(item.id)
        expected_status = "superseded" if old is not None and old.status == "rejected" else item.status
        if (
            existing is not None
            and edge_signature(existing) == edge_signature(item)
            and existing.expression == item.expression
            and existing.status == expected_status
            and all((existing.attributes or {}).get(k) == v for k, v in item.attributes.items())
        ):
            reuse.add(item.id)
        elif existing is not None:
            remap[item.id] = new_id()
    # 重复文件可复用版本；上下文改变时追加新边，并以新 ID 连接 hops，不改封链业务字段。
    for item in folded.edges:
        old_id = item.id
        paths = item.attributes.get("paths", [item.attributes.get("hops", [])])
        if any(hop in remap for path in paths for hop in path):
            if old_id in existing_edges:
                reuse.discard(old_id)
                remap.setdefault(old_id, new_id())
            item.attributes = {
                **item.attributes,
                "hops": [remap.get(h, h) for h in item.attributes.get("hops", [])],
                "paths": [[remap.get(h, h) for h in path] for path in paths],
            }
        if old_id in remap:
            item.id = remap[old_id]
        item.attributes = {**item.attributes, "original_edge_id": old_id}
    if reused_ids:
        deterministic_ids = {
            c.lineage_edge_id
            for c in session.scalars(select(CandidateEdge).where(CandidateEdge.job_id.in_(reused_ids)))
            if c.origin in ("parsed", "folded")
        }
        stale_ids = [
            e.id
            for e in existing_edges.values()
            if e.id in deterministic_ids and e.id not in reuse and e.status == "active"
        ]
        if stale_ids:
            session.execute(
                update(LineageEdge)
                .where(LineageEdge.id.in_(stale_ids))
                .values(status="superseded", superseded_at=utcnow())
            )
            session.execute(
                update(CandidateEdge)
                .where(CandidateEdge.lineage_edge_id.in_(stale_ids), CandidateEdge.status == "written")
                .values(status="superseded")
            )
    for item in folded.edges:
        original_id = item.attributes.pop("original_edge_id")
        if original_id in reuse:
            continue
        stmt, job = statements_by_id[item.statement_id], jobs_by_id[item.job_id]
        values = {k: getattr(item, k) for k in (*EDGE_FIELDS, "expression")}
        old = previous_by_id.get(original_id)
        if old:
            matched.add(old.id)
        _persist_edge(
            session,
            actor,
            job,
            stmt,
            values,
            assets,
            candidate_origin="folded" if item.attributes.get("via") else "parsed",
            status=item.status,
            edge_id=item.id,
            attributes=item.attributes,
            previous=old,
        )
        totals["edges"] += old is None or old.status != "rejected"
        totals["folded_edges"] += bool(item.attributes.get("via"))
    for job in jobs_by_id.values():
        if job.id in reused_ids:
            continue
        inherited = len(old_counts[job.id] & matched)
        job.stats = {
            **job.stats,
            "inherited_decisions": inherited,
            "invalidated_decisions": len(old_counts[job.id] - matched),
        }
        if job.stats["previous_id"]:
            audit.record(
                session,
                actor,
                "import.revision",
                "import_job",
                job.id,
                previous_id=job.stats["previous_id"],
                replaced_edges=job.stats["replaced_edges"],
                inherited_decisions=inherited,
            )
    session.flush()
    stats = dict(totals)
    stats["statements"] = dict(Counter(s.status for s in statements_by_id.values()))
    stats["files"] = len(jobs_by_id)
    stats["statement_count"] = len(statements_by_id)
    stats["pending_candidates"] = 0
    stats["unmatched_tables"] = _unmatched(prepared, assets)
    stats["invalidated_decisions"] = sum(j.stats.get("invalidated_decisions", 0) for j in jobs_by_id.values())
    batch.stats = stats
    batch.notes = [*(batch.notes or []), *notes]
    batch.progress = {**batch.progress, "phase": "model", "files_total": len(prepared), "files_done": len(prepared)}
    batch.updated_at = utcnow()


def _unmatched(prepared: list[PreparedFile], assets: dict) -> list[dict]:
    # 来源：本批任何语句写入过的表都不算缺口；目标：只有本批建出（或改名得到）的表不算
    writes = [w for f in prepared for p in f.parsed if p for w in p.writes]
    produced = {
        "source": {w.target_ref for w in writes},
        "target": {w.target_ref for w in writes if w.created or w.attributes.get("op") in ("rename", "swap")},
    }
    refs = {}
    for file in prepared:
        for stmt, parsed in zip(file.statements, file.parsed):
            if parsed is None:
                continue
            pairs = set()
            for write in parsed.writes:
                pairs.add((write.target_ref, "target"))
                pairs.update((e.source_ref, "source") for e in write.edges)
            for ref, role in pairs:
                if ref in assets or ref in produced[role]:
                    continue
                entry = refs.setdefault((ref, role), {"ref": ref, "role": role, "count": 0, "statement_ids": []})
                entry["count"] += 1
                if len(entry["statement_ids"]) < 5:
                    entry["statement_ids"].append(stmt.id)
    return sorted(refs.values(), key=lambda e: (e["ref"], e["role"]))


def _assist(
    batch_id: str,
    prepared: list[PreparedFile],
    contents: dict,
    schema: dict,
    dialect: Any,
    assets: dict,
    *,
    execution_job_id: str | None = None,
    attempt: int | None = None,
):
    from avelis.importer.assist import AssistBudget, run_assistance

    budget = AssistBudget()
    for file in prepared:
        if file.reused:
            continue
        try:
            with session_scope() as session:
                batch = session.get(ImportBatch, batch_id)
                _check_attempt(session, execution_job_id, attempt)
                job = session.get(ImportJob, file.job.id)
                if job.superseded_by is not None:
                    batch.notes = [*(batch.notes or []), f"文件 {job.filename} 已被新版本替换，跳过模型辅助"]
                    continue
                statements = list(
                    session.scalars(select(ExtractedStatement).where(ExtractedStatement.job_id == job.id))
                )
                policy = modelcfg.policy(session)
                resolved = modelcfg.resolve(session, "import") if policy.get("send_code") else None
                options, cached = copy.deepcopy(batch.options), copy.deepcopy(job.stats.get("assist_cache", {}))
                statement_contexts = copy.deepcopy(job.stats.get("statement_contexts", []))
            # 模型调用不持有元数据库事务或 Job 锁；回写前重新检查当前版本与执行权。
            result = run_assistance(
                language=job.language,
                content=contents[job.id],
                rule_statements=statements,
                dialect=dialect,
                schema=schema,
                default_db=job.default_db,
                options=options,
                policy=policy,
                resolved=resolved,
                budget=budget,
                cached=cached,
                statement_contexts=statement_contexts,
            )
            with session_scope() as session:
                batch = session.get(ImportBatch, batch_id)
                _check_attempt(session, execution_job_id, attempt)
                session.scalar(select(Datasource).where(Datasource.id == batch.datasource_id).with_for_update())
                job, actor = session.get(ImportJob, file.job.id), session.get(User, batch.created_by)
                if job.superseded_by is not None:
                    batch.notes = [*(batch.notes or []), f"文件 {job.filename} 已被新版本替换，模型结果未写入"]
                    continue
                previous = session.get(ImportJob, job.stats["previous_id"]) if job.stats.get("previous_id") else None
                index, _, old_candidates = _previous_data(session, previous)
                statements = list(
                    session.scalars(select(ExtractedStatement).where(ExtractedStatement.job_id == job.id))
                )
                by_id = {s.id: s for s in statements}
                for virtual in result.statements:
                    data = dict(virtual)
                    temp_id = data.pop("id")
                    data.setdefault("seq", len(by_id) + 1)
                    data.setdefault("placeholders", [])
                    data.setdefault("writes", [])
                    stmt = ExtractedStatement(id=new_id(), job_id=job.id, **data)
                    stmt.text_hash = normalized_text_hash(
                        stmt.raw_text, dialect.name, sql=stmt.method in ("sql", "python_ast", "shell")
                    )
                    session.add(stmt)
                    by_id[temp_id] = stmt
                for stmt_id, changes in result.statement_updates.items():
                    if stmt_id in by_id:
                        for key, value in changes.items():
                            setattr(by_id[stmt_id], key, value)
                matched = set()
                for values in result.candidates:
                    stmt = by_id.get(values.get("statement_id"))
                    old = index.get((stmt.text_hash, edge_signature(values))) if stmt else None
                    if old:
                        _persist_edge(
                            session,
                            actor,
                            job,
                            stmt,
                            values,
                            assets,
                            candidate_origin=values["origin"],
                            previous=old,
                            attributes=values.get("evidence"),
                        )
                        matched.add(old.id)
                    else:
                        session.add(
                            CandidateEdge(
                                id=new_id(),
                                job_id=job.id,
                                statement_id=stmt.id if stmt else None,
                                **{k: values.get(k) for k in (*EDGE_FIELDS, "expression", "confidence")},
                                origin=values["origin"],
                                evidence=values.get("evidence", {}),
                                status="pending",
                            )
                        )
                job.model_payload = result.payloads
                for payload in job.model_payload:
                    if payload.get("statement_id") in by_id:
                        payload["statement_id"] = by_id[payload["statement_id"]].id
                job.model_adapter = resolved.adapter if resolved else "demo"
                old_decisions = {c.id for c in old_candidates if c.status in ("accepted", "modified", "rejected")}
                inherited = job.stats.get("inherited_decisions", 0) + len(matched)
                job.stats = {
                    **job.stats,
                    "assist_cache": result.cache,
                    "inherited_decisions": inherited,
                    "invalidated_decisions": max(0, len(old_decisions) - inherited),
                    "notes": [*job.stats.get("notes", []), *result.notes],
                }
                if previous is not None and matched:
                    audit.record(
                        session,
                        actor,
                        "import.revision",
                        "import_job",
                        job.id,
                        phase="model",
                        previous_id=previous.id,
                        replaced_edges=0,
                        inherited_decisions=len(matched),
                        inherited_decisions_total=inherited,
                    )
                batch.notes = [*(batch.notes or []), *result.notes]
                suggested = set((batch.stats or {}).get("suggested_sql_functions", []))
                batch.stats = {
                    **batch.stats,
                    "suggested_sql_functions": sorted(suggested | set(result.suggested_sql_functions)),
                }
                session.flush()
                refresh_status(session, batch, keep_running=True)
        except Exception as exc:  # noqa: BLE001 - 单文件辅助失败不阻断后续文件
            with session_scope() as session:
                _check_attempt(session, execution_job_id, attempt)
                live = session.get(ImportBatch, batch_id)
                message = modelcfg.redact_error(f"{type(exc).__name__}: {exc}", limit=200)
                live.notes = [*(live.notes or []), f"文件 {file.job.filename} 的模型辅助失败：{message}"]


def _check_attempt(session: Session, job_id: str | None, attempt: int | None) -> None:
    if job_id is None:
        return
    job = session.scalar(select(Job).where(Job.id == job_id).with_for_update())
    if job is None or job.status != "running" or (attempt is not None and job.attempts != attempt):
        raise ImportAbandoned("后台导入任务已经结束或执行权已变更，本次结果未写入")


def run_import(batch_id: str, *, job_id: str | None = None, attempt: int | None = None) -> dict[str, Any]:
    with session_scope() as session:
        imported = session.get(ImportBatch, batch_id)
        actor_id = imported.created_by if imported is not None else None
    with events.failure_guard("lineage_import.failed", actor_id, "import_batch", batch_id, job_id=job_id):
        return _run_import(batch_id, job_id=job_id, attempt=attempt)


def _run_import(batch_id: str, *, job_id: str | None = None, attempt: int | None = None) -> dict[str, Any]:
    """worker 入口；模型失败不会回滚已经提交的确定性结果。"""
    try:
        with session_scope() as session:
            _check_attempt(session, job_id, attempt)
            batch = batch_or_404(session, batch_id)
            if batch.status not in ("queued", "running"):
                return {"batch_id": batch.id, "status": batch.status}
            batch.status, batch.updated_at = "running", utcnow()
            files = list(
                session.scalars(
                    select(ImportJob).where(ImportJob.batch_id == batch.id).order_by(ImportJob.created_at, ImportJob.id)
                )
            )
            duplicate_ids = (batch.options or {}).get("duplicate_file_ids", [])
            reused_statements = {}
            if duplicate_ids:
                files.extend(session.scalars(select(ImportJob).where(ImportJob.id.in_(duplicate_ids))))
                for reused_id in duplicate_ids:
                    reused_statements[reused_id] = {
                        s.seq: s
                        for s in session.scalars(
                            select(ExtractedStatement).where(ExtractedStatement.job_id == reused_id)
                        )
                    }
            files = list({j.id: j for j in files if not (j.stats or {}).get("duplicate_of")}.values())
            batch.progress = {**(batch.progress or {}), "files_total": len(files)}
            order = _file_order(batch)
            files.sort(key=lambda j: order.get(j.id, (j.stats or {}).get("upload_order", 0)))
            contents = {
                j.id: session.scalar(select(ImportSource.content).where(ImportSource.job_id == j.id)) for j in files
            }
            ds = session.get(Datasource, batch.datasource_id)
            dialect, separators = dialect_of(ds), engine_of(ds).script_separators
            schema, assets = build_schema(session, batch.datasource_id), _asset_map(session, batch.datasource_id)
        model_schema = copy.deepcopy(schema)
        prepared, notes = _prepare(batch, files, contents, schema, dialect, separators, model_schema, reused_statements)
        with session_scope() as session:
            _check_attempt(session, job_id, attempt)
            # 复核会在持有数据源锁时刷新引用批次；阶段二也必须先锁数据源，再锁批次。
            session.scalar(select(Datasource).where(Datasource.id == batch.datasource_id).with_for_update())
            live = session.scalar(select(ImportBatch).where(ImportBatch.id == batch_id).with_for_update())
            actor = session.get(User, live.created_by)
            _persist_prepared(session, actor, live, prepared, assets, notes)
        # 模型调用逐文件隔离；外发、缓存与校验由 assist 控制。
        try:
            _assist(
                batch_id, prepared, contents, model_schema, dialect, assets, execution_job_id=job_id, attempt=attempt
            )
        except Exception as exc:  # noqa: BLE001 - 保留确定性结果，辅助失败只记说明
            with session_scope() as session:
                _check_attempt(session, job_id, attempt)
                live = session.get(ImportBatch, batch_id)
                live.notes = [*(live.notes or []), f"模型辅助失败：{modelcfg.redact_error(str(exc), limit=200)}"]
        with session_scope() as session:
            # batch_or_404 会协调失败状态；必须在它可能写批次之前锁住 Job。
            _check_attempt(session, job_id, attempt)
            live = batch_or_404(session, batch_id)
            if live.status != "failed":
                refresh_status(session, live)
            live.progress = {
                **live.progress,
                "phase": "failed" if live.status == "failed" else "done",
                "current_file": None,
            }
            if live.status != "failed":
                audit.record(
                    session,
                    session.get(User, live.created_by) if live.created_by else None,
                    "import.completed",
                    "import_batch",
                    live.id,
                    status=live.status,
                    files=(live.stats or {}).get("files", 0),
                    statements=(live.stats or {}).get("statement_count", 0),
                    edges=(live.stats or {}).get("edges", 0),
                    failed=(live.stats or {}).get("statements", {}).get("failed", 0),
                )
            return {"batch_id": live.id, "status": live.status}
    except ImportAbandoned:
        raise
    except Exception as exc:
        failure_event_recorded = False
        try:
            with session_scope() as session:
                _check_attempt(session, job_id, attempt)
                if job_id is None:
                    # 同步调用没有 attempt fence，仍按关联 Job → ImportBatch 的顺序加锁。
                    associated_job_id = session.scalar(select(ImportBatch.job_id).where(ImportBatch.id == batch_id))
                    if associated_job_id:
                        session.scalar(select(Job).where(Job.id == associated_job_id).with_for_update())
                failed = session.scalar(
                    select(ImportBatch)
                    .where(ImportBatch.id == batch_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if failed is not None and failed.status in ("queued", "running"):
                    failed.status, failed.updated_at = "failed", utcnow()
                    failed.error = modelcfg.redact_error(f"{type(exc).__name__}: {exc}", limit=500)
                    failed.progress = {**(failed.progress or {}), "phase": "failed", "current_file": None}
                    events.record_failure(
                        session,
                        failed.created_by,
                        "lineage_import.failed",
                        "import_batch",
                        batch_id,
                        error_code=type(exc).__name__,
                        batch_id=batch_id,
                        job_id=job_id or failed.job_id,
                        status="failed",
                    )
                failure_event_recorded = failed is not None and failed.status == "failed"
            # 只有状态与事件成功提交后才通知 guard；事务失败仍由它独立留底。
            if failure_event_recorded:
                exc.failure_event_recorded = True
        except ImportAbandoned:
            raise
        except Exception:  # 不让状态收尾失败掩盖原异常；外层 guard 仍会尝试独立审计
            log.error("导入失败状态未能写入 %s", batch_id)
        raise


def refresh_status(
    session: Session, batch: ImportBatch, *, keep_running: bool = False, counts_only: bool = False
) -> None:
    files = [
        j
        for j in session.scalars(select(ImportJob).where(ImportJob.batch_id == batch.id))
        if not (j.stats or {}).get("duplicate_of")
    ]
    ids = list(dict.fromkeys([*[f.id for f in files], *(batch.options or {}).get("duplicate_file_ids", [])]))
    statements = list(session.scalars(select(ExtractedStatement).where(ExtractedStatement.job_id.in_(ids))))
    candidates = list(session.scalars(select(CandidateEdge).where(CandidateEdge.job_id.in_(ids))))
    pending = Counter(c.job_id for c in candidates if c.status == "pending")
    for file in files:
        status_counts = Counter(s.status for s in statements if s.job_id == file.id)
        file.stats = {**file.stats, "statements": dict(status_counts)}
        if not counts_only and file.superseded_by is None:
            file.status = (
                "needs_params" if status_counts["needs_params"] else "needs_review" if pending[file.id] else "done"
            )
    counts = Counter(s.status for s in statements)
    if not counts_only:
        batch.status = (
            "running"
            if keep_running
            else "needs_params"
            if counts["needs_params"]
            else "needs_review"
            if sum(pending.values())
            else "done"
        )
    edges = list(
        session.scalars(
            select(LineageEdge).where(
                LineageEdge.import_job_id.in_(ids), LineageEdge.status.in_(("active", "intermediate"))
            )
        )
    )
    batch.stats = {
        **(batch.stats or {}),
        "statements": dict(counts),
        "statement_count": len(statements),
        "pending_candidates": sum(pending.values()),
        "edges": len(edges),
        "active_edges": sum(e.status == "active" for e in edges),
        "intermediate_edges": sum(e.status == "intermediate" for e in edges),
        "folded_edges": sum(e.status == "active" and bool((e.attributes or {}).get("via")) for e in edges),
        "invalidated_decisions": sum(f.stats.get("invalidated_decisions", 0) for f in files),
    }
    batch.updated_at = utcnow()


def batch_or_404(session: Session, batch_id: str) -> ImportBatch:
    batch = session.get(ImportBatch, batch_id)
    if batch is None:
        raise NotFound("导入批次不存在")
    reconcile_batch(session, batch)
    return batch


def reconcile_batch(session: Session, batch: ImportBatch) -> None:
    """调用方事务负责提交（含 GET 的 get_session）；谁改 failed，谁同事务写事件。

    与 worker、回收统一先锁 Job 再锁批次，并刷新身份映射，避免旧快照重复转移。
    """
    if batch.status not in ("queued", "running") or not batch.job_id:
        return
    job = session.scalar(
        select(Job)
        .where(Job.id == batch.job_id)
        .with_for_update()
        .execution_options(autoflush=False, populate_existing=True)
    )
    if job is not None and job.status == "failed":
        batch = session.scalar(
            select(ImportBatch)
            .where(ImportBatch.id == batch.id)
            .with_for_update()
            .execution_options(autoflush=False, populate_existing=True)
        )
        if batch is None or batch.status not in ("queued", "running"):
            return
        batch.status, batch.updated_at = "failed", utcnow()
        batch.error = modelcfg.redact_error(job.error or "后台导入任务失败", limit=500)
        batch.progress = {**(batch.progress or {}), "phase": "failed", "current_file": None}
        events.record_failure(
            session,
            batch.created_by,
            "lineage_import.failed",
            "import_batch",
            batch.id,
            error_code="WORKER_FAILED",
            batch_id=batch.id,
            job_id=job.id,
            status="failed",
        )


def batch_view(session: Session, batch: ImportBatch) -> dict[str, Any]:
    reconcile_batch(session, batch)
    files = [
        j
        for j in session.scalars(
            select(ImportJob).where(ImportJob.batch_id == batch.id).order_by(ImportJob.created_at, ImportJob.id)
        )
        if not (j.stats or {}).get("duplicate_of")
    ]
    duplicate_ids = (batch.options or {}).get("duplicate_file_ids", [])
    if duplicate_ids:
        files.extend(session.scalars(select(ImportJob).where(ImportJob.id.in_(duplicate_ids))))
    files = list({j.id: j for j in files}.values())
    order = _file_order(batch)
    files.sort(key=lambda j: order.get(j.id, (j.stats or {}).get("upload_order", 0)))
    return {
        "id": batch.id,
        "datasource_id": batch.datasource_id,
        "default_db": batch.default_db,
        "status": batch.status,
        "progress": batch.progress,
        "options": batch.options,
        "params": (batch.options or {}).get("params", {}),
        "stats": batch.stats,
        "notes": batch.notes,
        "error": batch.error,
        "job_id": batch.job_id,
        "created_at": batch.created_at.isoformat(),
        "updated_at": batch.updated_at.isoformat(),
        "unmatched_tables": (batch.stats or {}).get("unmatched_tables", []),
        "files": [
            {
                "id": f.id,
                "filename": f.filename,
                "source_key": f.source_key,
                "revision": f.revision,
                "previous_id": (f.stats or {}).get("previous_id"),
                "supersedes": (f.stats or {}).get("previous_id"),
                "stats": f.stats,
                "superseded_by": f.superseded_by,
                "language": f.language,
                "status": batch.status if (f.stats or {}).get("staged") else f.status,
                "statements": (f.stats or {}).get("statements", {}),
                "statement_count": sum((f.stats or {}).get("statements", {}).values()),
            }
            for f in files
        ],
    }
