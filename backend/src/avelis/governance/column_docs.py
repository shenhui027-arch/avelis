"""字段含义以数据库的列注释（COMMENT）为准：Avelis 不在后端为字段另起显示名，而是原样展示列名并标出注释状态，
让缺失尽早暴露，反向推动数据库侧补齐注释。

状态：
- ok         本列有注释；
- linked     外部表 / Catalog 表：经身份映射取源表同名列的注释（含义在源库维护，不算缺口）；
- inherited  视图列没有注释，但直接取值的上游列有：本列缺注释，建议在视图上补（例如沿用上游注释）；
- missing    本列与上游都没有注释。
inherited 与 missing 都计为“注释缺失”。

继承与映射只沿查看者能看到的上游：上游资产对查看者不可见时，不沿用它的注释、不返回来源，本列记为 missing
（can_see 由调用方按当前用户给出，见 visible_to）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis.models import Asset, ColumnVersion, IdentityLink, LineageEdge, User

CanSee = Callable[[Asset], bool]


def visible_to(session: Session, user: User) -> CanSee:
    """查看者能否看到某个资产（与资产详情、问数来源权限同一规则）。"""
    from avelis.governance import access

    allowed = access.domain_ids(session, user)
    return lambda asset: access.can_read_asset(session, user, asset, allowed)

GAPS = ("inherited", "missing")
EXTERNAL = ("external_table", "catalog_table")


def _qualified(asset: Asset) -> str:
    return f"{asset.catalog_name}.{asset.db_name}.{asset.name}"


def _own_comment(session: Session, asset: Asset, column: str) -> str | None:
    if not asset.current_version_id:
        return None
    comment = session.scalar(select(ColumnVersion.comment).where(
        ColumnVersion.asset_version_id == asset.current_version_id, ColumnVersion.name == column))
    comment = (comment or "").strip()
    # 与列名相同的注释等于没有解释
    return comment if comment and comment.lower() != column.lower() else None


def column_doc(session: Session, asset: Asset, column: str, *, can_see: CanSee, _depth: int = 0) -> dict[str, Any]:
    out: dict[str, Any] = {"asset_id": asset.id, "asset": _qualified(asset), "column": column, "comment": None,
                           "status": "missing", "source": None}
    own = _own_comment(session, asset, column)
    if own:
        return {**out, "comment": own, "status": "ok"}
    if _depth > 8:
        return out
    if asset.kind in EXTERNAL:
        link = session.scalar(select(IdentityLink).where(IdentityLink.source_asset_id == asset.id,
                                                         IdentityLink.superseded_at.is_(None)))
        target = session.get(Asset, link.target_asset_id) if link and link.target_asset_id else None
        if link and link.status in ("confirmed", "candidate") and target is not None and can_see(target):
            upstream = column_doc(session, target, column, can_see=can_see, _depth=_depth + 1)
            if upstream["comment"]:
                return {**out, "comment": upstream["comment"], "status": "linked",
                        "source": f"{upstream['asset']}.{column}"}
        return out
    if asset.kind == "view" and asset.current_version_id:
        sources = list(session.scalars(select(LineageEdge).where(
            LineageEdge.target_asset_id == asset.id, LineageEdge.status == "active", LineageEdge.origin == "parsed",
            LineageEdge.target_asset_version_id == asset.current_version_id, LineageEdge.target_column == column,
            LineageEdge.relation == "value_direct")))
        if len(sources) == 1 and sources[0].source_asset_id and sources[0].source_column:
            src = session.get(Asset, sources[0].source_asset_id)
            upstream = (column_doc(session, src, sources[0].source_column, can_see=can_see, _depth=_depth + 1)
                        if src is not None and can_see(src) else None)
            if upstream and upstream["comment"]:
                return {**out, "comment": upstream["comment"], "status": "inherited",
                        "source": f"{upstream['asset']}.{sources[0].source_column}"}
    return out


def asset_docs(session: Session, asset: Asset, *, can_see: CanSee) -> list[dict[str, Any]]:
    if not asset.current_version_id:
        return []
    names = session.scalars(select(ColumnVersion.name).where(
        ColumnVersion.asset_version_id == asset.current_version_id).order_by(ColumnVersion.ordinal))
    return [column_doc(session, asset, name, can_see=can_see) for name in names]


def summarize(docs: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {s: 0 for s in ("ok", "linked", "inherited", "missing")}
    for d in docs:
        counts[d["status"]] += 1
    total = len(docs)
    documented = counts["ok"] + counts["linked"]
    return {**counts, "total": total, "gaps": counts["inherited"] + counts["missing"],
            "completeness": round(documented / total, 4) if total else None}
