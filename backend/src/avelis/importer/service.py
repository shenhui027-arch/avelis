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

from avelis import audit
from avelis.db import utcnow
from avelis.governance import access
from avelis.governance.errors import Conflict, Invalid, NotFound
from avelis.i18n import Msg
from avelis.importer.candidates import Candidate
from avelis.lineage.grain import resolve_qualified
from avelis.models import CandidateEdge, ExtractedStatement, ImportBatch, ImportJob, LineageEdge, User

LANGUAGES = {".sql": "sql", ".py": "python", ".sh": "shell", ".bash": "shell"}
CONFIRMED_ORIGINS = ("import", "import_manual", "model_confirmed")
RELATIONS = ("value_direct", "value_computed", "value_aggregated", "filter", "join_key", "group_by", "table")


def require_import_access(session: Session, actor: User) -> None:
    """导入原文、参数和模型载荷统一按维护者权限读写。"""
    access.require_role(session, actor, "steward")


def detect_language(filename: str, source_type: str | None) -> str:
    if source_type in ("sql", "python", "shell"):
        return source_type
    return LANGUAGES.get(os.path.splitext(filename)[1].lower(), "sql")


def _qualify(ref: str, default_db: str | None, catalog: str) -> str | None:
    parts = [p for p in ref.strip("`").replace("`", "").split(".") if p]
    if len(parts) == 3:
        return ".".join(parts)
    if len(parts) == 2:
        return f"{catalog}.{parts[0]}.{parts[1]}"
    if len(parts) == 1 and default_db:
        return f"{catalog}.{default_db}.{parts[0]}"
    return None


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
                       expression=expression, created_by=actor.id, reason=reason, status="active",
                       attributes={"lines": evidence.get("lines"), "statement_id": stmt.id if stmt else None,
                                   "job": job.filename, "design_time": True,
                                   **{k: evidence[k] for k in ("via", "hops", "paths", "op", "carried_from")
                                      if k in evidence}})
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


def _validate(c: Candidate, known: dict[str, set[str]], default_db: str | None, catalog: str,
              snippet: str) -> str | None:
    if c.relation not in RELATIONS:
        return f"关系类型 {c.relation} 不合法"
    source, target = _qualify(c.source_ref, default_db, catalog), _qualify(c.target_ref, default_db, catalog)
    if source is None or source not in known:
        return f"来源表 {c.source_ref} 未采集，也不是本脚本产生的表"
    if c.source_column and c.source_column not in known[source]:
        return f"来源表 {c.source_ref} 没有列 {c.source_column}"
    if target is None or target.split(".")[-1] not in snippet:
        return f"目标表 {c.target_ref} 未出现在代码中"
    return None


# --- 参数、人工处理 -----------------------------------------------------------------

def _refresh_referencing_batches(session: Session, job: ImportJob) -> None:
    from avelis.importer.batch import refresh_status

    session.flush()
    for imported in session.scalars(select(ImportBatch).where(ImportBatch.datasource_id == job.datasource_id)):
        if imported.id == job.batch_id or job.id in (imported.options or {}).get("duplicate_file_ids", []):
            refresh_status(session, imported, counts_only=imported.status in ("queued", "running"))


def decide(session: Session, actor: User, candidate: CandidateEdge, decision: str, reason: str,
           changes: dict[str, Any]) -> CandidateEdge:
    require_import_access(session, actor)
    if decision not in ("accept", "reject", "modify"):
        raise Invalid("decision 只能是 accept、reject 或 modify")
    if not reason:
        raise Invalid("必须填写处理理由")
    if candidate.status == "intermediate":
        raise Conflict("中间段只用于查证，请处理折叠后的边")
    job = session.get(ImportJob, candidate.job_id)
    if job.status == "superseded" or job.superseded_by is not None:
        raise Conflict("候选所属文件已被新版本替换，不能再处理")
    stmt = session.get(ExtractedStatement, candidate.statement_id) if candidate.statement_id else None
    if candidate.status not in ("pending", "written"):
        raise Conflict(Msg("候选当前状态为 {status}，不能再处理", status=candidate.status))
    if candidate.status == "written" and decision == "accept":
        raise Conflict("解析边已写入，无需接受")
    now = utcnow()
    old_edge = session.get(LineageEdge, candidate.lineage_edge_id) if candidate.lineage_edge_id else None
    if candidate.status == "written" and (old_edge is None or old_edge.status != "active"):
        if old_edge is not None and old_edge.status == "intermediate":
            raise Conflict("中间段只用于查证，请处理折叠后的边")
        raise Conflict("解析边当前不是 active，不能再处理")
    if candidate.status == "written":  # 修改或驳回已写入的解析边：旧边失效
        session.execute(update(LineageEdge).where(LineageEdge.id == candidate.lineage_edge_id,
                                                 LineageEdge.status == "active")
                        .values(status="superseded", superseded_at=now))
    if decision == "reject":
        candidate.status = "rejected"
    else:
        values = {k: changes.get(k) or getattr(candidate, k)
                  for k in ("source_ref", "source_column", "target_ref", "target_column", "relation")}
        if values["relation"] not in RELATIONS:
            raise Invalid(Msg("关系类型 {relation} 不合法", relation=values["relation"]))
        origin = "model_confirmed" if candidate.origin in ("model", "model_sql") else "import_manual"
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
    _refresh_referencing_batches(session, job)
    return candidate


def add_manual_edge(session: Session, actor: User, job: ImportJob, values: dict[str, Any], reason: str):
    require_import_access(session, actor)
    if values.get("relation") not in RELATIONS:
        raise Invalid("关系类型不合法")
    edge = _write_edge(session, actor, job, None, origin="import_manual", source_ref=values["source_ref"],
                       source_column=values.get("source_column"), target_ref=values["target_ref"],
                       target_column=values.get("target_column"), relation=values["relation"], expression=None,
                       candidate_origin="manual", confidence=None, evidence={"lines": values.get("lines")},
                       reason=reason)
    _refresh_referencing_batches(session, job)
    return edge


def job_or_404(session: Session, job_id: str) -> ImportJob:
    job = session.get(ImportJob, job_id)
    if job is None:
        raise NotFound("导入任务不存在")
    return job
