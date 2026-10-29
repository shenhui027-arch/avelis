"""导入血缘：上传 SQL 文件、粘贴 SQL、上传代码 → 抽取 → 确定性解析 → 模型候选 → 人工处理。

写入 lineage_edge 的 origin：
  import          确定性解析得到（直接写入，可修改）
  import_manual   人工补充或修改
  model_confirmed 模型候选经人工接受或修改后写入
模型候选在处理前只在 candidate_edge 中，不计入已确认血缘。导入的边是设计时血缘：
不参与字段推导指纹，不覆盖数据库观察到的定义。同一数据源下内容相同的导入幂等。
"""

from __future__ import annotations

import os
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from avelis import audit, modelcfg
from avelis.ask.llm import DEMO_ADAPTER
from avelis.collect.service import build_schema
from avelis.connectors.base import sha256_text
from avelis.db import utcnow
from avelis.governance import access
from avelis.governance.errors import Conflict, Invalid, NotFound
from avelis.importer.candidates import Candidate, demo_candidates, model_candidates, redact
from avelis.importer.extract import Segment, extract_python, extract_shell, split_sql, substitute
from avelis.importer.statements import parse_statement
from avelis.lineage.grain import resolve_qualified
from avelis.models import CandidateEdge, Datasource, ExtractedStatement, ImportJob, ImportSource, LineageEdge, User

LANGUAGES = {".sql": "sql", ".py": "python", ".sh": "shell", ".bash": "shell"}
CONFIRMED_ORIGINS = ("import", "import_manual", "model_confirmed")
RELATIONS = ("value_direct", "value_computed", "value_aggregated", "filter", "join_key", "group_by", "table")
DEFAULT_CATALOG = "default_catalog"


def detect_language(filename: str, source_type: str | None) -> str:
    if source_type in ("sql", "python", "shell"):
        return source_type
    return LANGUAGES.get(os.path.splitext(filename)[1].lower(), "sql")


def create_import(session: Session, actor: User, *, datasource_id: str, filename: str, content: str,
                  source_type: str | None, default_db: str | None, params: dict[str, str]) -> tuple[ImportJob, bool]:
    access.require_role(session, actor, "steward")
    if session.get(Datasource, datasource_id) is None:
        raise NotFound("数据源不存在")
    if not content.strip():
        raise Invalid("导入内容为空")
    digest = sha256_text(content)
    existing = session.scalar(select(ImportJob).where(ImportJob.datasource_id == datasource_id,
                                                      ImportJob.content_hash == digest))
    if existing is not None:
        audit.record(session, actor, "import.duplicate", "import_job", existing.id, filename=filename)
        return existing, False
    language = detect_language(filename, source_type)
    job = ImportJob(datasource_id=datasource_id, source_type=source_type or language, filename=filename,
                    language=language, dialect="starrocks", default_db=default_db, content_hash=digest,
                    status="processing", params=dict(params), created_by=actor.id)
    session.add(job)
    session.flush()
    session.add(ImportSource(job_id=job.id, content=content, content_hash=digest))
    session.flush()
    process(session, actor, job, content)
    audit.record(session, actor, "import.create", "import_job", job.id, filename=filename, language=language,
                 stats=job.stats)
    return job, True


def _segments(language: str, content: str) -> tuple[list[Segment], list[str]]:
    if language == "python":
        return extract_python(content)
    if language == "shell":
        return extract_shell(content)
    return split_sql(content), []


def _qualify(ref: str, default_db: str | None) -> str | None:
    parts = [p for p in ref.strip("`").replace("`", "").split(".") if p]
    if len(parts) == 3:
        return ".".join(parts)
    if len(parts) == 2:
        return f"{DEFAULT_CATALOG}.{parts[0]}.{parts[1]}"
    if len(parts) == 1 and default_db:
        return f"{DEFAULT_CATALOG}.{default_db}.{parts[0]}"
    return None


def process(session: Session, actor: User, job: ImportJob, content: str, *, only: set[int] | None = None) -> None:
    """解析导入内容。only 为需要（重新）写入的语句序号；其余语句只重放以重建脚本内的表结构。"""
    schema = build_schema(session, job.datasource_id)
    default_db = job.default_db
    segments, notes = _segments(job.language, content)
    existing = {s.seq: s for s in session.scalars(select(ExtractedStatement).where(
        ExtractedStatement.job_id == job.id))}
    stats: dict[str, Any] = {"segments": len(segments), "notes": notes, "parsed_edges": 0}
    for seq, seg in enumerate(segments, start=1):
        write = only is None or seq in only
        stmt = existing.get(seq)
        if stmt is None:
            stmt = ExtractedStatement(job_id=job.id, seq=seq, line_start=seg.line_start, line_end=seg.line_end,
                                      method=seg.method, raw_text=seg.text, placeholders=seg.placeholders,
                                      status="pending")
            session.add(stmt)
            session.flush()
        if seg.method == "unrecognized":
            if write:
                stmt.status = "model"
                stmt.reason = "确定性解析无法覆盖（非 SQL 的数据操作），已生成模型候选"
                _model_candidates(session, job, stmt, schema, default_db)
            continue
        sql, missing = substitute(seg.text, job.params)
        if missing:
            stmt.status, stmt.sql_text = "needs_params", None
            stmt.reason = f"动态 SQL，需要补充参数：{', '.join(missing)}"
            continue
        parsed = parse_statement(sql, schema=schema, default_db=default_db)
        if parsed.use_db:
            default_db = parsed.use_db
        if parsed.target_ref and parsed.target_columns:
            catalog, db, name = parsed.target_ref.split(".")
            schema.setdefault(catalog, {}).setdefault(db, {})[name] = {c: "VARCHAR" for c in parsed.target_columns}
        if not write:
            continue
        stmt.sql_text = sql
        stmt.statement_type = parsed.statement_type
        stmt.target_ref = parsed.target_ref
        stmt.status = parsed.status
        stmt.reason = parsed.reason
        for edge in parsed.edges:
            _write_edge(session, actor, job, stmt, origin="import", source_ref=edge.source_ref,
                        source_column=edge.source_column, target_ref=parsed.target_ref,
                        target_column=edge.target_column, relation=edge.relation, expression=edge.expression,
                        candidate_origin="parsed", confidence=None,
                        evidence={"lines": [seg.line_start, seg.line_end], "temporary_target": parsed.temporary})
            stats["parsed_edges"] += 1
    session.flush()
    all_statements = list(session.scalars(select(ExtractedStatement).where(ExtractedStatement.job_id == job.id)))
    pending = session.scalar(select(CandidateEdge.id).where(CandidateEdge.job_id == job.id,
                                                            CandidateEdge.status == "pending").limit(1))
    if any(s.status == "needs_params" for s in all_statements):
        job.status = "needs_params"
    elif pending:
        job.status = "needs_review"
    else:
        job.status = "done"
    by_status: dict[str, int] = {}
    for s in all_statements:
        by_status[s.status] = by_status.get(s.status, 0) + 1
    job.stats = {**(job.stats or {}), **stats, "statements": by_status}
    job.updated_at = utcnow()


def _write_edge(session: Session, actor: User, job: ImportJob, stmt: ExtractedStatement | None, *, origin: str,
                source_ref: str, source_column: str | None, target_ref: str, target_column: str | None,
                relation: str, expression: str | None, candidate_origin: str, confidence: float | None,
                evidence: dict[str, Any], reason: str | None = None, record_candidate: bool = True) -> LineageEdge:
    source = resolve_qualified(session, job.datasource_id, source_ref)
    target = resolve_qualified(session, job.datasource_id, target_ref)
    edge = LineageEdge(origin=origin, import_job_id=job.id, target_asset_id=target.id if target else None,
                       target_ref=target_ref, target_asset_version_id=target.current_version_id if target else None,
                       target_column=target_column, source_asset_id=source.id if source else None,
                       source_ref=source_ref, source_column=source_column, relation=relation,
                       expression=expression, created_by=actor.id, reason=reason,
                       attributes={"lines": evidence.get("lines"), "statement_id": stmt.id if stmt else None,
                                   "job": job.filename, "design_time": True})
    session.add(edge)
    session.flush()
    if not record_candidate:
        return edge
    candidate = CandidateEdge(job_id=job.id, statement_id=stmt.id if stmt else None, origin=candidate_origin,
                              source_ref=source_ref, source_column=source_column, target_ref=target_ref,
                              target_column=target_column, relation=relation, expression=expression,
                              confidence=confidence, evidence=evidence, status="written", lineage_edge_id=edge.id,
                              decided_by=actor.id if candidate_origin != "parsed" else None,
                              decided_at=utcnow() if candidate_origin != "parsed" else None, reason=reason)
    session.add(candidate)
    return edge


def _known_tables(schema: dict) -> dict[str, set[str]]:
    return {f"{c}.{d}.{t}": set(cols) for c, dbs in schema.items() for d, tables in dbs.items()
            for t, cols in tables.items()}


def _model_candidates(session: Session, job: ImportJob, stmt: ExtractedStatement, schema: dict,
                      default_db: str | None) -> None:
    known = _known_tables(schema)
    redacted, hits = redact(stmt.raw_text)
    policy = modelcfg.policy(session)
    resolved = modelcfg.resolve(session, "import") if policy["send_code"] else None
    adapter = resolved.adapter if resolved else DEMO_ADAPTER
    sent_to = adapter if resolved else "未发送（演示规则）"
    note = None
    if resolved is None and modelcfg.resolve(session, "import") is not None:
        note = "模型配置未允许发送代码片段，改用演示规则"
    payload = {"statement_id": stmt.id, "lines": [stmt.line_start, stmt.line_end], "text": redacted,
               "redactions": hits, "sent_to": sent_to, "note": note}
    job.model_payload = [*(job.model_payload or []), payload]
    job.model_adapter = adapter

    def columns_of(table: str) -> set[str]:
        q = _qualify(table, default_db)
        return known.get(q or "", set())

    try:
        if resolved:
            raw = model_candidates(resolved.model, redacted, stmt.line_start,
                                   {t: c for t, c in known.items() if t.split(".", 1)[1] in redacted
                                    or t.split(".")[-1] in redacted})
        else:
            raw = demo_candidates(redacted, stmt.line_start, columns_of)
    except Exception as exc:  # 模型失败如实记录，不影响确定性结果
        stmt.reason = f"模型推断失败：{type(exc).__name__}: {str(exc)[:200]}"
        payload["error"] = stmt.reason
        job.model_payload = [*job.model_payload[:-1], payload]
        return
    dropped = []
    for c in raw:
        problem = _validate(c, known, default_db, redacted)
        if problem:
            dropped.append({"edge": f"{c.source_ref}.{c.source_column} → {c.target_ref}.{c.target_column}",
                            "reason": problem})
            continue
        session.add(CandidateEdge(
            job_id=job.id, statement_id=stmt.id, origin="model", source_ref=_qualify(c.source_ref, default_db),
            source_column=c.source_column, target_ref=_qualify(c.target_ref, default_db),
            target_column=c.target_column, relation=c.relation, confidence=c.confidence,
            evidence={"lines": c.evidence_lines, "rationale": c.rationale, "adapter": adapter,
                      "is_demo": resolved is None}, status="pending"))
    if dropped:
        job.stats = {**(job.stats or {}), "dropped_candidates": [*job.stats.get("dropped_candidates", []),
                                                                  *dropped]}


def _validate(c: Candidate, known: dict[str, set[str]], default_db: str | None, snippet: str) -> str | None:
    if c.relation not in RELATIONS:
        return f"关系类型 {c.relation} 不合法"
    source, target = _qualify(c.source_ref, default_db), _qualify(c.target_ref, default_db)
    if source is None or source not in known:
        return f"来源表 {c.source_ref} 未采集，也不是本脚本产生的表"
    if c.source_column and c.source_column not in known[source]:
        return f"来源表 {c.source_ref} 没有列 {c.source_column}"
    if target is None or target.split(".")[-1] not in snippet:
        return f"目标表 {c.target_ref} 未出现在代码中"
    return None


# --- 参数、人工处理 -----------------------------------------------------------------

def set_params(session: Session, actor: User, job: ImportJob, params: dict[str, str]) -> ImportJob:
    access.require_role(session, actor, "steward")
    job.params = {**(job.params or {}), **{k: str(v) for k, v in params.items()}}
    waiting = {s.seq for s in session.scalars(select(ExtractedStatement).where(
        ExtractedStatement.job_id == job.id, ExtractedStatement.status == "needs_params"))}
    content = session.scalar(select(ImportSource.content).where(ImportSource.job_id == job.id))
    process(session, actor, job, content, only=waiting)
    audit.record(session, actor, "import.params", "import_job", job.id, params=sorted(params))
    return job


def decide(session: Session, actor: User, candidate: CandidateEdge, decision: str, reason: str,
           changes: dict[str, Any]) -> CandidateEdge:
    access.require_role(session, actor, "steward")
    if decision not in ("accept", "reject", "modify"):
        raise Invalid("decision 只能是 accept、reject 或 modify")
    if not reason:
        raise Invalid("必须填写处理理由")
    job = session.get(ImportJob, candidate.job_id)
    stmt = session.get(ExtractedStatement, candidate.statement_id) if candidate.statement_id else None
    if candidate.status not in ("pending", "written"):
        raise Conflict(f"候选当前状态为 {candidate.status}，不能再处理")
    if candidate.status == "written" and decision == "accept":
        raise Conflict("解析边已写入，无需接受")
    now = utcnow()
    if candidate.status == "written":  # 修改或驳回已写入的解析边：旧边失效
        session.execute(update(LineageEdge).where(LineageEdge.id == candidate.lineage_edge_id)
                        .values(status="superseded", superseded_at=now))
    if decision == "reject":
        candidate.status = "rejected"
    else:
        values = {k: changes.get(k) or getattr(candidate, k)
                  for k in ("source_ref", "source_column", "target_ref", "target_column", "relation")}
        if values["relation"] not in RELATIONS:
            raise Invalid(f"关系类型 {values['relation']} 不合法")
        origin = "model_confirmed" if candidate.origin == "model" else "import_manual"
        edge = _write_edge(session, actor, job, stmt, origin=origin, expression=candidate.expression,
                           candidate_origin=candidate.origin, confidence=candidate.confidence,
                           evidence={**candidate.evidence, "from_candidate": candidate.id}, reason=reason,
                           record_candidate=False, **values)
        candidate.status = "modified" if decision == "modify" else "accepted"
        candidate.lineage_edge_id = edge.id
        candidate.modified = {k: v for k, v in values.items() if v != getattr(candidate, k)}
    candidate.decided_by, candidate.decided_at, candidate.reason = actor.id, now, reason
    session.flush()
    if job.status == "needs_review" and not session.scalar(select(CandidateEdge.id).where(
            CandidateEdge.job_id == job.id, CandidateEdge.status == "pending").limit(1)):
        job.status = "done"
    audit.record(session, actor, f"import.candidate_{decision}", "candidate_edge", candidate.id, job_id=job.id,
                 reason=reason, changes=candidate.modified)
    return candidate


def add_manual_edge(session: Session, actor: User, job: ImportJob, values: dict[str, Any], reason: str):
    access.require_role(session, actor, "steward")
    if values.get("relation") not in RELATIONS:
        raise Invalid("关系类型不合法")
    return _write_edge(session, actor, job, None, origin="import_manual", source_ref=values["source_ref"],
                       source_column=values.get("source_column"), target_ref=values["target_ref"],
                       target_column=values.get("target_column"), relation=values["relation"], expression=None,
                       candidate_origin="manual", confidence=None, evidence={"lines": values.get("lines")},
                       reason=reason)


def job_view(session: Session, job: ImportJob) -> dict[str, Any]:
    statements = session.scalars(select(ExtractedStatement).where(ExtractedStatement.job_id == job.id)
                                 .order_by(ExtractedStatement.seq))
    candidates = session.scalars(select(CandidateEdge).where(CandidateEdge.job_id == job.id)
                                 .order_by(CandidateEdge.created_at))
    return {
        "id": job.id, "filename": job.filename, "language": job.language, "source_type": job.source_type,
        "datasource_id": job.datasource_id, "default_db": job.default_db, "status": job.status,
        "params": job.params, "stats": job.stats, "model_adapter": job.model_adapter,
        "model_payload": job.model_payload, "content_hash": job.content_hash, "created_at": job.created_at.isoformat(),
        "statements": [{"id": s.id, "seq": s.seq, "lines": [s.line_start, s.line_end], "method": s.method,
                        "statement_type": s.statement_type, "target": s.target_ref, "status": s.status,
                        "reason": s.reason, "placeholders": s.placeholders, "raw_text": s.raw_text,
                        "sql_text": s.sql_text} for s in statements],
        "candidates": [{"id": c.id, "statement_id": c.statement_id, "origin": c.origin, "status": c.status,
                        "source": f"{c.source_ref}.{c.source_column}" if c.source_column else c.source_ref,
                        "target": f"{c.target_ref}.{c.target_column}" if c.target_column else c.target_ref,
                        "source_ref": c.source_ref, "source_column": c.source_column, "target_ref": c.target_ref,
                        "target_column": c.target_column, "relation": c.relation, "expression": c.expression,
                        "confidence": c.confidence, "evidence": c.evidence, "lineage_edge_id": c.lineage_edge_id,
                        "modified": c.modified, "reason": c.reason, "decided_by": c.decided_by}
                       for c in candidates],
    }


def job_or_404(session: Session, job_id: str) -> ImportJob:
    job = session.get(ImportJob, job_id)
    if job is None:
        raise NotFound("导入任务不存在")
    return job
