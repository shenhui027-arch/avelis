"""变更留底：变更记录检索与导出、哈希链核验、保留期状态（ADR 0008）。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from avelis import audit
from avelis.api import schemas as S
from avelis.api.deps import current_user, get_session
from avelis.config import get_settings
from avelis.db import session_factory
from avelis.governance import access
from avelis.history import integrity, records
from avelis.models import AuditEvent, HistoryAnchor, User

router = APIRouter(prefix="/api", tags=["变更留底"])


def _csv(value: str | None) -> list[str] | None:
    items = [v.strip() for v in (value or "").split(",") if v.strip()]
    return items or None


def _filter(
    datasource_id: str | None,
    asset_id: str | None,
    column: str | None,
    kind: str | None,
    since: datetime | None,
    until: datetime | None,
    actor: str | None,
    attribution_status: str | None,
) -> records.EventFilter:
    def naive(t: datetime | None) -> datetime | None:  # 元数据库存不带时区的 UTC；不带时区的输入按 UTC
        return t.astimezone(timezone.utc).replace(tzinfo=None) if t and t.tzinfo else t

    return records.EventFilter(
        datasource_id=datasource_id,
        asset_id=asset_id,
        column=column,
        kinds=_csv(kind),
        since=naive(since),
        until=naive(until),
        actor=actor,
        attribution_status=_csv(attribution_status),
    )


@router.get("/change-events", response_model=S.ChangeEventPage)
def list_change_events(
    datasource_id: str | None = None,
    asset_id: str | None = None,
    column: str | None = None,
    kind: str | None = Query(None, description="变更类型，逗号分隔"),
    since: datetime | None = Query(None, description="发现时间下限（含）；不带时区按 UTC"),
    until: datetime | None = Query(None, description="发现时间上限（不含）；不带时区按 UTC"),
    actor: str | None = Query(None, description="数据库用户"),
    attribution_status: str | None = Query(None, description="归因结论，逗号分隔"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    f = _filter(datasource_id, asset_id, column, kind, since, until, actor, attribution_status)
    return records.search(session, user, f, limit, offset)


@router.get("/change-events/export")
def export_change_events(
    format: Literal["csv", "json"] = "csv",
    datasource_id: str | None = None,
    asset_id: str | None = None,
    column: str | None = None,
    kind: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    actor: str | None = None,
    attribution_status: str | None = None,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> Response:
    f = _filter(datasource_id, asset_id, column, kind, since, until, actor, attribution_status)
    snap = session_factory()()  # 计数、链头与事件取自同一个只读快照；导出审计另写
    try:
        snap.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        snap.execute(text("SET TRANSACTION READ ONLY"))
        body, manifest = records.export(snap, user, f, format)
    except records.ExportTooLarge as exc:
        raise HTTPException(413, exc.args[0]) from exc
    finally:
        snap.rollback()
        snap.close()
    audit.record(
        session,
        user,
        "history.export",
        "change_event",
        None,
        format=format,
        filters=f.describe(),
        rows=manifest["rows"],
        content_sha256=manifest["content_sha256"],
        chain_head=manifest["chain_head"],
    )
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    head = manifest["chain_head"] or {}
    headers = {
        "Content-Disposition": f'attachment; filename="avelis-change-events-{stamp}.{format}"',
        "X-Avelis-Export-Rows": str(manifest["rows"]),
        "X-Avelis-Export-Sha256": manifest["content_sha256"],
        "X-Avelis-Chain-Head": f"{head.get('seq')}:{head.get('hash')}",
        "Access-Control-Expose-Headers": "Content-Disposition, X-Avelis-Export-Rows, X-Avelis-Export-Sha256, "
        "X-Avelis-Chain-Head",
    }
    media = "application/json" if format == "json" else "text/csv; charset=utf-8"
    return Response(content=body, media_type=media, headers=headers)


@router.get("/history/verify", response_model=S.HistoryVerifyOut)
def verify_history(user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    """核验哈希链（管理员）。在独立的 REPEATABLE READ 事务中读同一快照；只读，不修复。"""
    access.require_role(session, user, "admin")
    check = session_factory()()
    try:
        check.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        report = integrity.verify(check)
    finally:
        check.rollback()
        check.close()
    audit.record(
        session,
        user,
        "history.verify",
        "history",
        None,
        ok=report["ok"],
        issue_count=report["issue_count"],
        head=report["head"],
        first_break=report["first_break"],
    )
    return report


@router.get("/history/status", response_model=S.HistoryStatusOut)
def history_status(user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    access.require_role(session, user, "admin", "steward")
    settings = get_settings()
    last = session.scalar(
        select(AuditEvent)
        .where(AuditEvent.action.in_(("history.purge", "history.purge_refused")))
        .order_by(AuditEvent.at.desc())
        .limit(1)
    )
    anchors = session.scalars(select(HistoryAnchor).order_by(HistoryAnchor.at.desc()).limit(10))
    return {
        "retention_days": settings.history_retention_days,
        "purge_interval_hours": settings.history_purge_interval_hours,
        "export_max_rows": settings.history_export_max_rows,
        "chain_head": integrity.chain_head(session),
        "chained_rows": session.execute(text("SELECT last_seq FROM history_chain_head WHERE id = 1")).scalar(),
        "last_purge": {
            "at": last.at.isoformat(),
            "action": last.action,
            "detail": json.loads(json.dumps(last.detail, default=str)),
        }
        if last
        else None,
        "anchors": [
            {
                "id": a.id,
                "at": a.at.isoformat(),
                "cutoff": a.cutoff.isoformat(),
                "counts": a.counts,
                "gaps": len(a.gaps or []),
                "chain_seq": a.chain_seq,
            }
            for a in anchors
        ],
    }
