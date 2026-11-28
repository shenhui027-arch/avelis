"""验证查询库（VQR / Golden SQL）：只读 SELECT 校验、域级可见性、严格防自审审批流与有界召回。"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

import sqlglot
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlglot import exp

from avelis import audit
from avelis.db import new_id, utcnow
from avelis.governance import access
from avelis.governance.errors import Conflict, Forbidden, Invalid, NotFound
from avelis.models import AnalysisRun, Answer, AuditEvent, Datasource, Domain, VerifiedQuery
from avelis.retrieval_hybrid import SearchDocument, hybrid_rank

VALID_STATUSES = ("draft", "pending_review", "approved", "rejected", "archived")
_DISALLOWED_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Alter,
    exp.Create,
    exp.Command,
    exp.Merge,
    exp.Into,
    exp.Lock,
)
_DIALECT_ALIASES = {
    "postgresql": "postgres",
    "postgresql+psycopg": "postgres",
    "starrocks": "mysql",
    "doris": "mysql",
    "tidb": "mysql",
    "sqlserver": "tsql",
    "mssql": "tsql",
}


def validate_vqr_sql(sql_text: str, dialect: str = "postgres") -> str:
    """校验 VQR SQL 为单条只读 SELECT（含 CTE/UNION），拒绝 DDL/DML 与多语句。"""
    cleaned = (sql_text or "").strip().rstrip(";").strip()
    if not cleaned:
        raise Invalid("验证查询 SQL 不能为空")
    read_dialect = _DIALECT_ALIASES.get((dialect or "").strip().lower(), (dialect or "").strip().lower() or "postgres")
    try:
        parsed = sqlglot.parse(cleaned, read=read_dialect)
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError, ValueError):
        try:
            parsed = sqlglot.parse(cleaned, read="postgres")
        except (sqlglot.errors.ParseError, sqlglot.errors.TokenError, ValueError) as exc:
            raise Invalid(f"验证查询 SQL 语法无效：{exc}") from None
    if len(parsed) != 1 or parsed[0] is None:
        raise Invalid("验证查询 SQL 必须为单条 SELECT 语句")
    stmt = parsed[0]
    if not isinstance(stmt, (exp.Select, exp.Union)):
        raise Invalid("验证查询 SQL 仅支持只读 SELECT / UNION 查询")
    for node_type in _DISALLOWED_NODES:
        if stmt.find(node_type) is not None:
            raise Invalid("验证查询 SQL 禁止包含写入或 DDL 语句")
    truncate_cls = getattr(exp, "TruncateTable", None)
    if truncate_cls is not None and stmt.find(truncate_cls) is not None:
        raise Invalid("验证查询 SQL 禁止包含 TRUNCATE 语句")
    return cleaned


def vqr_view(vq: VerifiedQuery) -> dict[str, Any]:
    return {
        "id": vq.id,
        "domain_id": vq.domain_id,
        "title": vq.title,
        "question": vq.question,
        "question_variants": list(vq.question_variants or []),
        "sql_text": vq.sql_text,
        "dialect": vq.dialect,
        "datasource_id": vq.datasource_id,
        "metric_codes": list(vq.metric_codes or []),
        "tags": list(vq.tags or []),
        "notes": vq.notes,
        "source_answer_id": vq.source_answer_id,
        "source_run_id": vq.source_run_id,
        "status": vq.status,
        "created_by": vq.created_by,
        "created_at": vq.created_at.isoformat(),
        "updated_at": vq.updated_at.isoformat(),
        "submitted_by": vq.submitted_by,
        "submitted_at": vq.submitted_at.isoformat() if vq.submitted_at else None,
        "reviewed_by": vq.reviewed_by,
        "reviewed_at": vq.reviewed_at.isoformat() if vq.reviewed_at else None,
        "review_note": vq.review_note,
        "approved_at": vq.approved_at.isoformat() if vq.approved_at else None,
    }


def _vqr_or_404(session: Session, vqr_id: str, *, lock: bool = False) -> VerifiedQuery:
    vq = session.get(VerifiedQuery, vqr_id, with_for_update=lock, populate_existing=lock)
    if vq is None:
        raise NotFound("验证查询不存在")
    return vq


def _can_manage_domain_vqr(session: Session, actor, domain_id: str) -> bool:
    if access.is_admin(session, actor):
        return True
    if access.domain_role(session, actor, domain_id) in ("owner", "maintainer"):
        return True
    return domain_id in access.domain_ids(session, actor) and bool(
        access.roles_of(session, actor) & {"steward", "reviewer"}
    )


def _vqr_doc(vq: VerifiedQuery) -> SearchDocument:
    variants = list(vq.question_variants or [])
    metric_codes = list(vq.metric_codes or [])
    tags = list(vq.tags or [])
    fields: list[tuple[float, str]] = [
        (3.0, vq.title),
        (3.0, vq.question),
        *((2.5, v) for v in variants),
        (2.0, " ".join(metric_codes)),
        (1.5, " ".join(tags)),
        (1.0, vq.notes or ""),
    ]
    semantic = " ".join(p for p in (vq.title, vq.question, *variants, *metric_codes, *tags, vq.notes or "") if p)
    return SearchDocument(
        key=vq.id,
        fields=fields,
        semantic_text=semantic,
        exact_tokens=tuple(metric_codes),
    )


def list_vqr(
    session: Session,
    actor,
    *,
    domain_id: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    access.require_role(session, actor, "analyst", "steward", "reviewer")
    if status is not None and status not in VALID_STATUSES:
        raise Invalid("无效的验证查询状态")
    stmt = (
        select(VerifiedQuery)
        .where(access.domain_visibility_clause(session, actor, VerifiedQuery.domain_id))
        .order_by(VerifiedQuery.updated_at.desc(), VerifiedQuery.id)
    )
    if domain_id:
        stmt = stmt.where(VerifiedQuery.domain_id == domain_id)
    if status:
        stmt = stmt.where(VerifiedQuery.status == status)
    rows = [vq for vq in session.scalars(stmt) if access.can_see_object(session, actor, "verified_query", vq.id)]
    query = (q or "").strip()
    if query and rows:
        by_id = {vq.id: vq for vq in rows}
        docs = [_vqr_doc(vq) for vq in rows]
        rows = [by_id[key] for key, score in hybrid_rank(query, docs) if score > 0.0]
    sliced = rows[offset : offset + max(limit, 1)]
    return [vqr_view(vq) for vq in sliced]


def get_vqr(session: Session, actor, vqr_id: str) -> dict[str, Any]:
    access.require_role(session, actor, "analyst", "steward", "reviewer")
    vq = _vqr_or_404(session, vqr_id)
    if not access.can_see_object(session, actor, "verified_query", vq.id):
        raise NotFound("验证查询不存在或无权访问")
    return vqr_view(vq)


def create_vqr(
    session: Session,
    actor,
    *,
    domain_id: str,
    title: str,
    question: str,
    sql_text: str,
    dialect: str = "postgres",
    question_variants: Iterable[str] = (),
    datasource_id: str | None = None,
    metric_codes: Iterable[str] = (),
    tags: Iterable[str] = (),
    notes: str | None = None,
    source_answer_id: str | None = None,
    source_run_id: str | None = None,
) -> dict[str, Any]:
    access.require_role(session, actor, "steward", "reviewer")
    domain = session.get(Domain, domain_id)
    if domain is None:
        raise NotFound("业务域不存在")
    if not _can_manage_domain_vqr(session, actor, domain_id):
        raise Forbidden("需要该业务域的维护或管理权限")
    if datasource_id and session.get(Datasource, datasource_id) is None:
        raise Invalid("引用数据源不存在")
    if source_answer_id:
        ans = session.get(Answer, source_answer_id)
        if ans is None:
            raise Invalid("引用问答结果不存在")
        if ans.user_id != actor.id and not access.is_admin(session, actor):
            raise Forbidden("无权引用他人问答结果")
    if source_run_id:
        run = session.get(AnalysisRun, source_run_id)
        if run is None:
            raise Invalid("引用分析运行不存在")
        if run.user_id != actor.id and not access.is_admin(session, actor):
            raise Forbidden("无权引用他人分析运行")
    validated_sql = validate_vqr_sql(sql_text, dialect)
    now = utcnow()
    vq = VerifiedQuery(
        id=new_id(),
        domain_id=domain_id,
        title=title.strip(),
        question=question.strip(),
        question_variants=[str(v).strip() for v in question_variants if str(v).strip()],
        sql_text=validated_sql,
        dialect=(dialect or "postgres").strip().lower(),
        datasource_id=datasource_id,
        metric_codes=[str(c).strip() for c in metric_codes if str(c).strip()],
        tags=[str(t).strip() for t in tags if str(t).strip()],
        notes=(notes or "").strip() or None,
        source_answer_id=source_answer_id,
        source_run_id=source_run_id,
        status="draft",
        created_by=actor.id,
        created_at=now,
        updated_at=now,
    )
    session.add(vq)
    session.flush()
    audit.record(session, actor, "vqr.create", "verified_query", vq.id, domain_id=domain_id, title=vq.title)
    return vqr_view(vq)


def update_vqr(session: Session, actor, vqr_id: str, **fields: Any) -> dict[str, Any]:
    access.require_role(session, actor, "steward", "reviewer")
    vq = _vqr_or_404(session, vqr_id, lock=True)
    if not access.is_admin(session, actor) and vq.domain_id not in access.domain_ids(session, actor):
        raise Forbidden("无权修改该验证查询")
    if vq.created_by != actor.id and not _can_manage_domain_vqr(session, actor, vq.domain_id):
        raise Forbidden("无权修改该验证查询")
    if vq.status not in ("draft", "rejected"):
        raise Conflict("只有草稿或已驳回状态的验证查询可以编辑")
    next_dialect = (fields.get("dialect") or vq.dialect or "postgres").strip().lower()
    if "sql_text" in fields and fields["sql_text"] is not None:
        vq.sql_text = validate_vqr_sql(fields["sql_text"], next_dialect)
    elif "dialect" in fields and fields["dialect"] is not None:
        validate_vqr_sql(vq.sql_text, next_dialect)
    vq.dialect = next_dialect
    if "title" in fields and fields["title"] is not None:
        vq.title = str(fields["title"]).strip()
    if "question" in fields and fields["question"] is not None:
        vq.question = str(fields["question"]).strip()
    if "question_variants" in fields and fields["question_variants"] is not None:
        vq.question_variants = [str(v).strip() for v in fields["question_variants"] if str(v).strip()]
    if "datasource_id" in fields:
        ds_id = fields["datasource_id"]
        if ds_id and session.get(Datasource, ds_id) is None:
            raise Invalid("引用数据源不存在")
        vq.datasource_id = ds_id
    if "metric_codes" in fields and fields["metric_codes"] is not None:
        vq.metric_codes = [str(c).strip() for c in fields["metric_codes"] if str(c).strip()]
    if "tags" in fields and fields["tags"] is not None:
        vq.tags = [str(t).strip() for t in fields["tags"] if str(t).strip()]
    if "notes" in fields:
        vq.notes = (fields["notes"] or "").strip() or None
    if vq.status == "rejected":
        vq.status = "draft"
        vq.reviewed_by = None
        vq.reviewed_at = None
        vq.review_note = None
    vq.updated_at = utcnow()
    session.flush()
    audit.record(session, actor, "vqr.update", "verified_query", vq.id, status=vq.status)
    return vqr_view(vq)


def submit_vqr(session: Session, actor, vqr_id: str) -> dict[str, Any]:
    access.require_role(session, actor, "steward", "reviewer")
    vq = _vqr_or_404(session, vqr_id, lock=True)
    if not access.is_admin(session, actor) and vq.domain_id not in access.domain_ids(session, actor):
        raise Forbidden("无权提交该验证查询")
    if vq.created_by != actor.id and not _can_manage_domain_vqr(session, actor, vq.domain_id):
        raise Forbidden("无权提交该验证查询")
    if vq.status not in ("draft", "rejected"):
        raise Conflict("只有草稿或已驳回状态的验证查询可以提交复核")
    validate_vqr_sql(vq.sql_text, vq.dialect)
    now = utcnow()
    vq.status = "pending_review"
    vq.submitted_by = actor.id
    vq.submitted_at = now
    vq.reviewed_by = None
    vq.reviewed_at = None
    vq.review_note = None
    vq.updated_at = now
    session.flush()
    audit.record(session, actor, "vqr.submit", "verified_query", vq.id, domain_id=vq.domain_id)
    return vqr_view(vq)


def _check_vqr_no_self_review(session: Session, actor, vq: VerifiedQuery, *, action_label: str) -> None:
    if actor.id in (vq.created_by, vq.submitted_by):
        raise Forbidden(f"禁止自审：创建人或提交人不能{action_label}同一条验证查询")
    since = vq.reviewed_at or vq.approved_at
    stmt = select(AuditEvent.actor_id).where(
        AuditEvent.object_type == "verified_query",
        AuditEvent.object_id == vq.id,
        AuditEvent.action == "vqr.update",
        AuditEvent.actor_id == actor.id,
    )
    if since is not None:
        stmt = stmt.where(AuditEvent.at >= since)
    if session.scalar(stmt.limit(1)) is not None:
        raise Forbidden(f"禁止自审：编辑人不能{action_label}同一条验证查询")


def approve_vqr(session: Session, actor, vqr_id: str, *, note: str | None = None) -> dict[str, Any]:
    access.require_role(session, actor, "reviewer")
    vq = _vqr_or_404(session, vqr_id, lock=True)
    if not access.is_admin(session, actor) and vq.domain_id not in access.domain_ids(session, actor):
        raise NotFound("验证查询不存在或无权访问")
    _check_vqr_no_self_review(session, actor, vq, action_label="审批")
    if vq.status != "pending_review":
        raise Conflict("只有待复核状态的验证查询可以批准")
    validate_vqr_sql(vq.sql_text, vq.dialect)
    now = utcnow()
    vq.status = "approved"
    vq.reviewed_by = actor.id
    vq.reviewed_at = now
    vq.review_note = (note or "").strip() or None
    vq.approved_at = now
    vq.updated_at = now
    session.flush()
    audit.record(session, actor, "vqr.approve", "verified_query", vq.id, note=vq.review_note)
    return vqr_view(vq)


def reject_vqr(session: Session, actor, vqr_id: str, *, note: str) -> dict[str, Any]:
    reason = (note or "").strip()
    if not reason:
        raise Invalid("驳回必须填写原因")
    access.require_role(session, actor, "reviewer")
    vq = _vqr_or_404(session, vqr_id, lock=True)
    if not access.is_admin(session, actor) and vq.domain_id not in access.domain_ids(session, actor):
        raise NotFound("验证查询不存在或无权访问")
    _check_vqr_no_self_review(session, actor, vq, action_label="驳回")
    if vq.status != "pending_review":
        raise Conflict("只有待复核状态的验证查询可以驳回")
    now = utcnow()
    vq.status = "rejected"
    vq.reviewed_by = actor.id
    vq.reviewed_at = now
    vq.review_note = reason
    vq.updated_at = now
    session.flush()
    audit.record(session, actor, "vqr.reject", "verified_query", vq.id, note=reason)
    return vqr_view(vq)


def archive_vqr(session: Session, actor, vqr_id: str) -> dict[str, Any]:
    access.require_role(session, actor, "steward", "reviewer")
    vq = _vqr_or_404(session, vqr_id, lock=True)
    if not _can_manage_domain_vqr(session, actor, vq.domain_id):
        raise Forbidden("需要该业务域的维护或管理权限")
    if vq.status != "approved":
        raise Conflict("只有已批准的验证查询可以归档")
    now = utcnow()
    vq.status = "archived"
    vq.updated_at = now
    session.flush()
    audit.record(session, actor, "vqr.archive", "verified_query", vq.id)
    return vqr_view(vq)


def recall_vqr(
    session: Session,
    actor,
    *,
    question: str = "",
    metric_codes: Iterable[str] = (),
    k: int = 4,
    char_limit: int = 2500,
) -> list[dict[str, Any]]:
    """召回当前用户可见的已批准 VQR（仅返回只读参考数据，绝不绕过治理编译或执行校验）。"""
    from avelis.ask.service import visible_metric

    q = (question or "").strip()
    wanted_codes = {str(c).strip() for c in metric_codes if str(c).strip()}
    if k <= 0 or char_limit <= 0 or (not q and not wanted_codes):
        return []
    try:
        allowed = access.domain_ids(session, actor)
        if not allowed:
            return []
        rows = [
            vq
            for vq in session.scalars(
                select(VerifiedQuery)
                .where(VerifiedQuery.status == "approved", VerifiedQuery.domain_id.in_(sorted(allowed)))
                .order_by(VerifiedQuery.updated_at.desc(), VerifiedQuery.id)
                .limit(200)
            )
            if isinstance(vq, VerifiedQuery)
        ]
    except Exception:
        return []
    if not rows:
        return []

    visible_rows: list[VerifiedQuery] = []
    for vq in rows:
        codes = [c for c in (vq.metric_codes or []) if c]
        if any(visible_metric(session, code, allowed)[1] is None for code in codes):
            continue
        visible_rows.append(vq)
    if not visible_rows:
        return []

    by_id = {vq.id: vq for vq in visible_rows}
    docs = [_vqr_doc(vq) for vq in visible_rows]
    ranked = hybrid_rank(q or " ".join(sorted(wanted_codes)), docs)
    scored: list[tuple[float, str, VerifiedQuery]] = []
    for vq_id, base_score in ranked:
        vq = by_id[vq_id]
        overlap = len(wanted_codes & set(vq.metric_codes or []))
        if q and base_score <= 0.0 and not overlap:
            continue
        if not q and not overlap:
            continue
        total = base_score + 0.15 * overlap
        scored.append((total, vq_id, vq))
    scored.sort(key=lambda item: (-round(item[0], 9), item[1]))

    selected: list[dict[str, Any]] = []
    used = 0
    for _, _, vq in scored:
        entry = {
            "id": vq.id,
            "title": vq.title,
            "question": vq.question,
            "sql_text": vq.sql_text,
            "dialect": vq.dialect,
            "metric_codes": list(vq.metric_codes or []),
            "notes": vq.notes,
        }
        cost = len(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
        if used + cost > char_limit:
            continue
        selected.append(entry)
        used += cost
        if len(selected) >= k:
            break
    return selected
