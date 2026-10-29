"""变更记录的检索与导出（ADR 0008 第 3 点）。

只返回当前用户可见资产的事件（与变更集详情同一规则）；没有资产的事件（如实例变化）只对管理员可见。
导出每行带链上 row_hash，清单带内容 SHA-256 与导出时的链头，便于事后核对。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, literal, or_, select
from sqlalchemy.orm import Session

from avelis.config import get_settings
from avelis.governance import access
from avelis.history.integrity import chain_head
from avelis.models import Asset, ChangeEvent, ChangeSet, Datasource, User


@dataclass
class EventFilter:
    datasource_id: str | None = None
    asset_id: str | None = None
    column: str | None = None
    kinds: list[str] | None = None
    since: datetime | None = None
    until: datetime | None = None
    actor: str | None = None
    attribution_status: list[str] | None = None

    def describe(self) -> dict[str, Any]:
        return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in self.__dict__.items() if v}


def _query(session: Session, user: User, f: EventFilter):
    stmt = (select(ChangeEvent, ChangeSet, Asset, Datasource.name)
            .join(ChangeSet, ChangeSet.id == ChangeEvent.change_set_id)
            .join(Datasource, Datasource.id == ChangeSet.datasource_id)
            .outerjoin(Asset, Asset.id == ChangeEvent.asset_id))
    if not access.is_admin(session, user):
        allowed = access.domain_ids(session, user)
        unassigned = (get_settings().unassigned_asset_policy == "allow"
                      or bool(access.roles_of(session, user) & {"admin", "steward"}))
        stmt = stmt.where(Asset.id.is_not(None), or_(Asset.domain_id.in_(allowed) if allowed else literal(False),
                                                     and_(Asset.domain_id.is_(None), literal(unassigned))))
    if f.datasource_id:
        stmt = stmt.where(ChangeSet.datasource_id == f.datasource_id)
    if f.asset_id:
        stmt = stmt.where(ChangeEvent.asset_id == f.asset_id)
    if f.column:
        stmt = stmt.where(ChangeEvent.column_name == f.column)
    if f.kinds:
        stmt = stmt.where(ChangeEvent.kind.in_(f.kinds))
    if f.since:
        stmt = stmt.where(ChangeEvent.created_at >= f.since)
    if f.until:
        stmt = stmt.where(ChangeEvent.created_at < f.until)
    if f.actor:
        stmt = stmt.where(ChangeEvent.actor_db_user == f.actor)
    if f.attribution_status:
        stmt = stmt.where(ChangeEvent.attribution_status.in_(f.attribution_status))
    return stmt


def _view(e: ChangeEvent, cs: ChangeSet, asset: Asset | None, ds_name: str) -> dict[str, Any]:
    return {
        "id": e.id, "change_set_id": cs.id, "datasource_id": cs.datasource_id, "datasource": ds_name,
        "asset_id": e.asset_id, "asset": f"{asset.catalog_name}.{asset.db_name}.{asset.name}" if asset else None,
        "column": e.column_name, "kind": e.kind, "before": e.before, "after": e.after,
        "detected_at": e.created_at.isoformat(),
        "window": {"after": cs.window_start.isoformat() if cs.window_start else None,
                   "before": cs.window_end.isoformat()},
        "actor_db_user": e.actor_db_user, "attribution_status": e.attribution_status,
        "attribution": e.attribution, "chain_seq": e.chain_seq, "row_hash": e.row_hash,
    }


def search(session: Session, user: User, f: EventFilter, limit: int, offset: int) -> dict[str, Any]:
    stmt = _query(session, user, f)
    total = session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = session.execute(stmt.order_by(ChangeEvent.created_at.desc(), ChangeEvent.id).limit(limit).offset(offset))
    return {"total": total, "limit": limit, "offset": offset, "items": [_view(*r) for r in rows],
            "actors": sorted(a for a in session.scalars(
                select(ChangeEvent.actor_db_user).where(ChangeEvent.actor_db_user.is_not(None)).distinct()))}


class ExportTooLarge(Exception):
    pass


CSV_COLUMNS = ("chain_seq", "row_hash", "detected_at", "datasource", "asset", "column", "kind", "actor_db_user",
               "attribution_status", "attribution_reason", "attribution_source", "window_after", "window_before",
               "before", "after", "change_set_id", "event_id")


def _csv_row(v: dict[str, Any]) -> list[Any]:
    return [v["chain_seq"], v["row_hash"], v["detected_at"], v["datasource"], v["asset"], v["column"], v["kind"],
            v["actor_db_user"], v["attribution_status"], (v["attribution"] or {}).get("reason"),
            (v["attribution"] or {}).get("source"), v["window"]["after"], v["window"]["before"],
            json.dumps(v["before"], ensure_ascii=False, sort_keys=True),
            json.dumps(v["after"], ensure_ascii=False, sort_keys=True), v["change_set_id"], v["id"]]


def export(session: Session, user: User, f: EventFilter, fmt: str) -> tuple[bytes, dict[str, Any]]:
    """返回 (文件内容, 清单)。CSV：数据部分（表头 + 行）的 SHA-256 为 content_sha256，清单以 # 开头的行附在末尾；
    JSON：events 数组按键排序、紧凑序列化后的 SHA-256。"""
    cap = get_settings().history_export_max_rows
    stmt = _query(session, user, f)
    total = session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    if total > cap:
        raise ExportTooLarge(f"符合条件的变更记录有 {total} 条，超过导出上限 {cap}（AVELIS_HISTORY_EXPORT_MAX_ROWS），"
                             "请缩小时间段或其他筛选条件")
    head = chain_head(session)
    items = [_view(*r) for r in session.execute(stmt.order_by(ChangeEvent.chain_seq.asc().nulls_last(),
                                                                ChangeEvent.id))]
    manifest: dict[str, Any] = {"format": fmt, "filters": f.describe(), "rows": len(items),
                                "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                                "exported_by": user.username, "chain_head": head,
                                "hash_algorithm": "sha256"}
    if fmt == "json":
        canonical = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        manifest["content_sha256"] = hashlib.sha256(canonical).hexdigest()
        manifest["content_rule"] = ("sha256(json.dumps(events, ensure_ascii=False, sort_keys=True, "
                                    "separators=(',', ':')))")
        body = json.dumps({"manifest": manifest, "events": items}, ensure_ascii=False, indent=1).encode()
        return body, manifest
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for v in items:
        writer.writerow(_csv_row(v))
    data = buf.getvalue().encode("utf-8-sig")  # 带 BOM：Excel 按 UTF-8 打开中文
    manifest["content_sha256"] = hashlib.sha256(data).hexdigest()
    manifest["content_rule"] = "sha256(去掉以 # 开头的清单行后的全部字节，含开头的 UTF-8 BOM)"
    trailer = "".join(f"# {k}: {json.dumps(v, ensure_ascii=False)}\n" for k, v in manifest.items())
    return data + trailer.encode(), manifest
